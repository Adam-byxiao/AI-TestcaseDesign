#!/usr/bin/env node
/**
 * AiWebTestCenter - 01 子项目编排脚本（零依赖，Node >= 18）
 *
 * 用法：
 *   node scripts/orchestrate.mjs init    --product <product> --version <vX.Y> [--prd <path>]
 *   node scripts/orchestrate.mjs validate --dir <casesDir>
 *   node scripts/orchestrate.mjs stats   --dir <casesDir>
 *
 * init     ：创建 output/<product>/<version>/{analysis,plan,cases/cases} 与 manifest.json，
 *            并从 PRD 文件头解析产品名/版本（可选）。
 * validate ：校验用例 JSON（字段、编号唯一、枚举、回溯格式）。
 * stats    ：输出用例统计（总数/模块/优先级/标签/REQ 覆盖）。
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

const HELP = `用法:
  node orchestrate.mjs init    --product <p> --version <v> [--prd <file>]
  node orchestrate.mjs validate --dir <casesDir>
  node orchestrate.mjs stats   --dir <casesDir>`

const MODULES = ['AUTH', 'HOME', 'PROD', 'SEARCH', 'CART', 'CHECKOUT', 'COUPON', 'ORDER', 'ACCOUNT', 'COMMENT', 'COMMON', 'PERF']
const PRIORITIES = ['P0', 'P1', 'P2', 'P3']
const ACTIONS = ['goto', 'fill', 'click', 'selectOption', 'check', 'hover', 'press', 'reload', 'expectVisible', 'expectText', 'expectUrl', 'waitFor']
const ID_RE = /^TC-[A-Z]+(-[A-Z0-9]+)?-\d{3}$/
const REQ_RE = /^REQ-[A-Z]+-\d{3}$/

function parseArgs(argv) {
  const args = {}
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a.startsWith('--')) {
      const key = a.slice(2)
      const next = argv[i + 1]
      if (next !== undefined && !next.startsWith('--')) {
        args[key] = next
        i++
      } else {
        args[key] = true
      }
    }
  }
  return args
}

function loadJson(file) {
  try {
    return JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, ''))
  } catch (e) {
    throw new Error(`无法解析 JSON: ${file} (${e.message})`)
  }
}

function listCaseFiles(dir) {
  if (!existsSync(dir)) throw new Error(`目录不存在: ${dir}`)
  return readdirSync(dir)
    .filter((f) => f.endsWith('.json'))
    .map((f) => join(dir, f))
}

/* ---------------- init ---------------- */
function cmdInit(args) {
  const product = args.product
  const version = args.version
  if (!product || !version) {
    console.error('init 需要 --product 与 --version')
    console.error(HELP)
    process.exit(1)
  }
  const root = resolve(join('output', product, version))
  const dirs = [join(root, 'analysis'), join(root, 'plan'), join(root, 'cases', 'cases')]
  for (const d of dirs) mkdirSync(d, { recursive: true })

  const manifest = { product, version, prd: null, createdAt: new Date().toISOString(), stats: null }

  if (args.prd && existsSync(args.prd)) {
    const head = readFileSync(args.prd, 'utf8').split('\n').slice(0, 10).join('\n')
    const mTitle = head.match(/^#\s+PRD[：:]\s*(.+?)(?:\s+v[\d.]+)?$/m)
    const mVer = head.match(/版本[：:]\s*(v[\d.]+)/)
    manifest.prd = resolve(args.prd)
    manifest.prdTitle = mTitle ? mTitle[1].trim() : null
    manifest.prdVersion = mVer ? mVer[1] : null
  } else if (args.prd) {
    console.warn(`[warn] PRD 文件不存在: ${args.prd}，manifest.prd 置空`)
  }

  const manifestPath = join(root, 'manifest.json')
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n')
  console.log(`已初始化: ${root}`)
  console.log(`  manifest: ${manifestPath}`)
  if (manifest.prdTitle) console.log(`  PRD: ${manifest.prdTitle} ${manifest.prdVersion ?? ''}`)
}

/* ---------------- validate ---------------- */
function validateCase(c, index, seenIds, errors) {
  const at = `[用例#${index + 1} ${c.id ?? '(无id)'}]`
  if (!c.id || typeof c.id !== 'string') {
    errors.push(`${at} 缺少 id`)
  } else {
    if (!ID_RE.test(c.id)) errors.push(`${at} id 格式非法: ${c.id}`)
    if (seenIds.has(c.id)) errors.push(`${at} id 重复: ${c.id}`)
    seenIds.add(c.id)
  }

  if (!MODULES.includes(c.module)) errors.push(`${at} module 非法: ${c.module ?? '(空)'}`)
  if (!PRIORITIES.includes(c.priority)) errors.push(`${at} priority 非法: ${c.priority ?? '(空)'}`)
  if (!Array.isArray(c.tags) || c.tags.length === 0) errors.push(`${at} tags 为空`)
  if (c.priority === 'P0' && !(Array.isArray(c.tags) && c.tags.includes('@smoke')))
    errors.push(`${at} P0 用例必须含 @smoke 标签`)
  if (!Array.isArray(c.requirement) || c.requirement.length === 0) errors.push(`${at} requirement 为空`)
  for (const r of c.requirement ?? []) if (!REQ_RE.test(r)) errors.push(`${at} requirement 格式非法: ${r}`)
  if (!Array.isArray(c.steps) || c.steps.length === 0) errors.push(`${at} steps 为空`)
  for (const s of c.steps ?? []) {
    if (!ACTIONS.includes(s.action)) errors.push(`${at} step.action 非法: ${s.action ?? '(空)'}`)
    if (!s.target || typeof s.target !== 'string') errors.push(`${at} step.target 为空`)
  }
  if (!Array.isArray(c.expected) || c.expected.length === 0) errors.push(`${at} expected 为空`)
  if (!c.title || typeof c.title !== 'string') errors.push(`${at} 缺少 title`)
}

function cmdValidate(args) {
  if (!args.dir) return console.error('validate 需要 --dir\n' + HELP), process.exit(1)
  const files = listCaseFiles(args.dir)
  if (files.length === 0) return console.log('未找到用例 JSON'), process.exit(1)
  const seenIds = new Set()
  const errors = []
  let count = 0
  for (const f of files) {
    let cases
    try {
      cases = loadJson(f)
    } catch (e) {
      errors.push(`[文件 ${f}] ${e.message}`)
      continue
    }
    if (!Array.isArray(cases)) {
      errors.push(`[文件 ${f}] 顶层必须是数组`)
      continue
    }
    cases.forEach((c, i) => validateCase(c, count + i, seenIds, errors))
    count += cases.length
  }
  if (errors.length > 0) {
    console.error(`校验失败：${errors.length} 个问题`)
    for (const e of errors) console.error('  ✗ ' + e)
    process.exit(1)
  }
  console.log(`校验通过：${count} 条用例，id 唯一，字段与枚举全部合法 ✔`)
}

/* ---------------- stats ---------------- */
function cmdStats(args) {
  if (!args.dir) return console.error('stats 需要 --dir\n' + HELP), process.exit(1)
  const files = listCaseFiles(args.dir)
  const all = []
  for (const f of files) {
    const cases = loadJson(f)
    if (Array.isArray(cases)) all.push(...cases)
  }
  if (all.length === 0) return console.log('未找到用例 JSON')

  const byModule = {}, byPriority = {}, byTag = {}, reqCovered = new Set()
  for (const c of all) {
    byModule[c.module] = (byModule[c.module] ?? 0) + 1
    byPriority[c.priority] = (byPriority[c.priority] ?? 0) + 1
    for (const t of c.tags ?? []) byTag[t] = (byTag[t] ?? 0) + 1
    for (const r of c.requirement ?? []) reqCovered.add(r)
  }
  console.log(`用例总数: ${all.length}`)
  console.log('按模块:', JSON.stringify(byModule, null, 2))
  console.log('按优先级:', JSON.stringify(byPriority, null, 2))
  console.log('按标签:', JSON.stringify(byTag, null, 2))
  console.log(`覆盖 REQ 数: ${reqCovered.size}`)
}

/* ---------------- main ---------------- */
const [cmd, ...rest] = process.argv.slice(2)
const args = parseArgs(rest)
switch (cmd) {
  case 'init': cmdInit(args); break
  case 'validate': cmdValidate(args); break
  case 'stats': cmdStats(args); break
  default:
    console.error(HELP)
    process.exit(cmd ? 1 : 0)
}
