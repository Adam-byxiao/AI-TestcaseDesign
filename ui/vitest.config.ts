import {defineConfig} from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins:[react()],
  test:{
    environment:'jsdom',
    setupFiles:['./src/test/setup.ts'],
    restoreMocks:true,
    // antd 组件在 jsdom 下首次渲染常需 1~3s（含 rc-* 动画与 ResizeObserver 兜底）；
    // 默认 5s 会让含多次 `findBy*` 的用例随机超时 —— 实测出现过 flaky，
    // 单独跑通过、与其他文件并发时失败。放宽到 20s 消除该噪声。
    testTimeout:20000,
    hookTimeout:20000,
  }
});
