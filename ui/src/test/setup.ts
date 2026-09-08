import '@testing-library/jest-dom/vitest';

Object.defineProperty(window,'matchMedia',{writable:true,value:()=>({matches:false,addListener:()=>{},removeListener:()=>{},addEventListener:()=>{},removeEventListener:()=>{},dispatchEvent:()=>false})});
globalThis.ResizeObserver=class {observe(){} unobserve(){} disconnect(){}};
const nativeGetComputedStyle=window.getComputedStyle;
window.getComputedStyle=(element:Element)=>nativeGetComputedStyle(element);
