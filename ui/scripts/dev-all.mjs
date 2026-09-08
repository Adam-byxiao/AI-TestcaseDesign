import {spawn} from 'node:child_process';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const uiDir=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const projectDir=resolve(uiDir,'..');
const python=process.env.PYTHON||(process.platform==='win32'?'python':'python3');
const separator=process.platform==='win32'?';':':';
const env={...process.env,PYTHONPATH:[resolve(projectDir,'src'),process.env.PYTHONPATH||''].filter(Boolean).join(separator)};
const children=[];
const run=(command,args,label,cwd)=>{const child=spawn(command,args,{cwd,env,stdio:'inherit',shell:process.platform==='win32'&&command.endsWith('.cmd')});children.push(child);child.on('error',(error)=>console.error(`${label} 启动失败: ${error.message}`));child.on('exit',(code)=>{if(code&&code!==0)console.error(`${label} exited with code ${code}`);if(label==='FastAPI'&&code)console.error('FastAPI 未启动，请先执行: pip install -e ".[service]"')})};
run(python,['-m','uvicorn','prd2tc.service:create_app','--factory','--host','127.0.0.1','--port','8000'],'FastAPI',projectDir);
run(process.platform==='win32'?'npm.cmd':'npm',['run','dev','--','--host','127.0.0.1'],'Vite',uiDir);
const shutdown=()=>{for(const child of children)child.kill('SIGTERM');setTimeout(()=>process.exit(0),200)};
process.on('SIGINT',shutdown);process.on('SIGTERM',shutdown);
