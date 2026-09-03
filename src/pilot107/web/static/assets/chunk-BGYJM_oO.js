import{c}from"./app.js";/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const m=c("File",[["path",{d:"M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z",key:"1rqfz7"}],["path",{d:"M14 2v4a2 2 0 0 0 2 2h4",key:"tnqrlb"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const d=c("Folder",[["path",{d:"M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z",key:"1kt360"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const g=c("X",[["path",{d:"M18 6 6 18",key:"1bl5f8"}],["path",{d:"m6 6 12 12",key:"d8bk6v"}]]);function a(e){const t=e.replace(/\/+$/,"");return t===""?"/":t}function i(e,t){const n=a(e);return n==="/"?`/${t}`:`${n}/${t}`}function u(e){const t=e.replace(/\/+$/,""),n=t.lastIndexOf("/");return n<=0?"/":t.slice(0,n)}function l(e){const t=e.split("/").filter(Boolean),n=[{label:"/",path:"/"}];let o="";for(const r of t)o+=`/${r}`,n.push({label:r,path:o});return n}function f(e,t){const n=a(e),o=a(t);return n===o||n.startsWith(o+"/")?n:o}function h(e){const t=[];for(const n of e.split("/"))if(!(n===""||n===".")){if(n===".."){t.pop();continue}t.push(n)}return`/${t.join("/")}`}function v(e,t,n){if(e.includes("\0"))throw new Error("路径包含无效字符");const o=e.trim();if(!o)throw new Error("请输入路径");const r=o.startsWith("/")?o:`${a(t)}/${o}`,s=h(r);if(f(s,n)!==s)throw new Error("路径超出授权目录");return s}function $(e,t){const n=l(a(e)),o=a(t),r=o==="/"?0:n.findIndex(s=>s.path===o);return r<0?[a(e)]:n.slice(r).map(s=>s.path)}function b(e,t){return e.includes(t)?e.filter(n=>n!==t):[...e,t]}function w(e){return e.map(t=>t.path)}function z(e,t){const n=new Set(t);return e.filter(o=>!n.has(o.path)).map(o=>o.path)}function k(e,t){const n=a(t),o=[];for(const r of e)a(u(r.path))!==n&&o.push({from:r.path,to:i(n,r.name),name:r.name});return o}function x(e){const t=e.toLowerCase();return[".tar",".gz",".tgz",".bz2",".xz",".zip",".rar"].some(n=>t.endsWith(n))}export{d as F,g as X,m as a,k as b,f as c,$ as d,l as e,z as f,x as i,i as j,a as n,u as p,v as r,w as s,b as t};
