# 107Pilot 前端视觉改造设计 v1.0

日期：2026-07-23  
状态：待实施  
范围：`apps/web` 的视觉、布局、动效、响应式与无障碍表达；不改变 API、业务状态机、权限模型或 Slurm 行为。

## 1. 设计目标

107Pilot 是一个证据优先的 HPC 工作台，而不是通用 BI 仪表盘。界面需要让用户始终清楚：

1. 当前位于什么环境；
2. 当前最需要处理的 Run / Agent / Contract 是什么；
3. 一个操作会影响哪个受控对象；
4. 结果是否具备可复核证据。

正式视觉方向为：**低饱和、丰满平面、锚定式浅层层级**。

- 平面设计是默认；色块、留白、排版和分割线承担分组；
- 只有当前主工作面、活动导航和临时控制层获得浅层深度；
- 不采用大面积玻璃拟态、霓虹、高饱和渐变、持续漂浮或装饰性粒子；
- 色彩表达语义与路径，不能成为识别状态的唯一方式；
- 环境边界“Docker Slurm simulator，非真实 107 平台”必须始终可见且不被视觉弱化。

## 2. 已冻结的约束

- 保持 React 18、TypeScript strict、Vite、TanStack Query、Lucide；不新增动效依赖；
- 保持 `/projects`、`/runs`、`/market`、`/studio/*`、`/agent`、`/cluster`、`/terminal` 路由与深链；
- 不修改后端 API schema、状态名称、轮询频率、owner-scope、可信身份边界或二次确认；
- 不把模拟器结果表现为真实 107 能力；
- 保持键盘操作、可见焦点、状态文本、`aria-live` 与 `prefers-reduced-motion` 支持。

## 3. 视觉系统

### 3.1 颜色

| 角色 | Token | 值 | 用途 |
| --- | --- | --- | --- |
| 画布 | `--canvas` | `#F4F6FA` | 页面背景 |
| 基础表面 | `--surface-base` | `#F8F9FC` | 工作区底面、分组背景 |
| 抬升表面 | `--surface-raised` | `#FFFFFF` | 当前主工作面、弹层 |
| 主文字 | `--ink` | `#17233D` | 标题、关键值 |
| 次文字 | `--ink-soft` | `#5D6C86` | 正文、说明 |
| 分割线 | `--line` | `#DBE2ED` | 平面分区 |
| 深海导航 | `--nav-start/end` | `#101A35 / #182245` | Sidebar、代码底色 |
| 主路径 | `--primary` | `#5866C8` | 当前导航、主 CTA、焦点 |
| 可信成功 | `--success` | `#23866C` | verified、fresh、succeeded |
| 环境/警告 | `--warning` | `#B57925` | simulator、需注意、pending |
| 风险/失败 | `--danger` | `#B75561` | failed、destructive action |
| 信息 | `--info` | `#3F7EAA` | 中性系统信息 |

主色只用于当前路径与关键操作；大面积使用画布、白色、深海导航与低饱和语义浅色。

### 3.2 表面和深度

| 层 | 名称 | 允许对象 | 视觉规则 |
| --- | --- | --- | --- |
| L0 | Canvas | 全页面 | 纯冷灰画布，不投影 |
| L1 | Work area | 列表、表单分区、事实表 | 平面色块与 `1px` 分割线；不投影 |
| L2 | Anchored work | 当前 Run、主要编辑区、推荐动作 | 白色表面，极浅阴影；不超过每屏 1–2 个 |
| L3 | Transient control | 活动 Tab、menu、确认面板、drawer | `--shadow-float`，只在交互期间出现 |

```css
--shadow-work: 0 1px 2px rgb(22 35 61 / 4%), 0 10px 24px rgb(22 35 61 / 6%);
--shadow-float: 0 10px 28px rgb(22 35 61 / 12%);
```

边界应优先采用：背景色差 → 内部分割线 → 边缘渐隐；只有真正可点击或需要聚焦的对象使用完整边框。Sidebar、粘性 topbar 和活动 tab 与内容区之间使用 12–16px 的渐隐阴影，而不是硬切割。

### 3.3 圆角、栅格与留白

- 统一使用对称圆角：控件 `8px`、工作面 `12px`、临时浮层 `14px`；
- 采用 4px 基线；主页面间距为 `24 / 32px`，主区内边距 `20–24px`；
- 以 12 栏流式栅格组织桌面内容；主工作区宽度优先于平均分栏；
- 不使用非对称圆角、剪裁形状或任意几何装饰。

## 4. 排版和跨平台渲染

### 4.1 字体栈

```css
--font-sans:
  ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei",
  "Noto Sans CJK SC", "Noto Sans SC", sans-serif;

--font-mono:
  "SFMono-Regular", "Cascadia Mono", "JetBrains Mono",
  "Noto Sans Mono CJK SC", Consolas, "Liberation Mono", monospace;
```

使用系统字体确保 macOS、Windows、Linux 和 Android 能选取本机最合适的字形；generic `sans-serif` / `monospace` 必须作为最终回退。后续若截图矩阵证明字面差异影响布局，再通过本地 WOFF2 + `unicode-range` 引入可控子集，不依赖远程字体。

### 4.2 字号阶梯

| 角色 | 尺寸 / 行高 | 权重 | 备注 |
| --- | --- | --- | --- |
| 页面标题 | `32/40px` 桌面，`28/36px` 手机 | 700 | 中文不使用负字距 |
| 区域标题 | `20/28px` | 700 | 每页只保留一个主标题层 |
| 卡片 / 主操作 | `16/22px` | 600 | 可操作名称 |
| 正文和按钮 | `15/23px` | 400–600 | 默认阅读尺寸 |
| 辅助事实 | `13/19px` | 400–500 | 时间、来源、短说明 |
| 标签 / 元数据 | `12/16px` | 600 | 不低于 12px |
| 代码 / ID | `13/20px` | 400–500 | 保持横向滚动，不缩小字号 |

仅使用 `400 / 500 / 600 / 700` 字重。所有文本通过 `rem` 和无单位 `line-height` 定义；不得用固定高度让文本垂直居中。`font-size-adjust` 只能作为支持它的现代浏览器的增强，不能作为旧设备布局正确性的前提。

## 5. 信息层级和页面布局

### 5.1 全局壳层

```text
Desktop
Sidebar 248px | Topbar: environment → API → user
               | Page header: title / intent / one primary action
               | Anchored primary work area
               | Flat secondary facts and history
```

- 环境边界位于 topbar 左侧，使用文字加琥珀状态点；
- Desktop sidebar 是广义导航层，可拥有轻深度；页面内容保持平面；
- Topbar 通过渐隐阴影与滚动内容过渡；
- 手机保留“工作台 / 作业 / Studio / 更多”四个一级目的地，其余通过 `更多` 打开，不压缩为 7 个 9px 标签。

### 5.2 工作台 `/projects`

优先级：**待处理 Run** → 推荐动作 → 近期活动 → 平台/授权事实。

- 左侧 1.7 份宽度为近期 Run；第一条需处理 Run 置顶并使用淡琥珀平面；
- 右侧 0.7 份宽度为“下一步”，`新建 Contract` 独占推荐动作区；
- 活动数量、可见分区等仅为紧凑支撑指标；
- 平台事实和授权卡片降至第二行，不与需处理工作竞争注意力。

### 5.3 作业 `/runs` 和 Run 详情

优先级：**当前阶段** → 可行动作 → 证据结果 → 历史细节。

- Run 列表为信息平面；仅选中 Run 打开 anchored detail；
- 生命周期采用横向状态轨道；`Evidence` / 当前阶段增宽，已完成阶段收紧；
- `Capsule verified`、失败诊断或可取消状态作为详情主事实面；
- timeline、lineage、raw data、原生命令进入分区或 disclosure，避免首次同时出现。

### 5.4 Contract Studio `/studio/*`

优先级：**canonical source** → 表单编辑 → Agent 建议 → validation / script / run launch。

- 桌面列宽：表单 `0.68fr`、源码 `1.38fr`、Agent `0.76fr`；
- 源码为 L2 anchored work，表单与 Agent 为 L1 平面辅助列；
- validation 和 script 仅在需要时展开；已持久化 Contract 才显示 `Preflight → Prepare → Confirm submit`；
- 任何 Agent 建议都必须呈现 diff 与人类确认，不通过视觉强度掩盖风险。

### 5.5 模板市场、Agent、集群、终端

- Market：用 trust、验证次数、成功率和 scope 建立卡片差异；采用操作固定在卡片末端；
- Agent：会话状态与人工接管优先于原始 audit；proposal diff 是 detail 主区；
- Cluster：按 capability / snapshot / entitlement 三个事实来源平面分组；时间与 freshness 紧邻对应事实；
- Terminal：深色代码面板仅在命令展示区；全局页面仍使用浅色平面。必须显示“复制，不执行”的边界。

## 6. 动效

### 6.1 页面切换

- 旧页：`opacity 1 → 0`，`translateX(0 → -12px)`，`180ms`；
- 新页：`opacity 0 → 1`，`translateX(18px → 0)`，`260ms`；
- 新页的 L2 主工作面额外 `translateY(4px → 0)`，`220ms`；
- 列表行、辅助事实和轮询数据不重复入场；页面高度不动画。

### 6.2 局部反馈

- 活动导航 / tab：背景与底边 160ms；
- Button hover：仅 `translateY(-1px)` + 轻阴影 140ms；press 80ms；
- 新 Evidence / finding：opacity 180ms，不对整张卡片重播；
- 只有状态点允许低频 pulse；badge 本体不闪烁；
- 禁止 `transition: all`、bounce、持续浮动、scale 文本。

### 6.3 减少动态

`prefers-reduced-motion` 下移除位移、缩放、stagger 与循环；页面仅保留 80ms opacity 更新，状态文本立即改变。

## 7. 响应式与可访问性

| 断点 | 规则 |
| --- | --- |
| `>= 1280px` | 完整 sidebar；按页面定义的主次列宽 |
| `900–1279px` | sidebar 可收窄；主工作面可保留两列；Studio 源码仍优先 |
| `< 900px` | 抽屉/底部导航；主次工作面堆叠；不裁切代码 |
| `< 520px` | 单列；次事实转为行；tabs 可横向滚动；操作按钮至少 44px 高 |

- 所有交互目标至少 `44 × 44px`；
- 状态有文本、图标与颜色；
- 主文字和语义色满足 WCAG AA；
- Run ID、hash、路径使用可复制的 `code` 并允许横向滚动；
- 大字体时取消非必要多栏，正文优先换行，次信息可折叠但不可丢失。

## 8. 改造计划

### Phase A — Foundations（不改业务逻辑）

1. 拆分 `styles.css` 为 tokens、base、shell、components、pages、motion；
2. 建立颜色、字号、surface、shadow、spacing、motion tokens；
3. 重构 `App.tsx` 壳层：sidebar、topbar、环境边界、移动导航；
4. 升级通用 `StatusBadge`、`SectionHeading`、`QueryBoundary`、按钮、列表行与空状态；
5. 补充字号与 long-text 回归用例。

### Phase B — High-frequency workflows

1. `/projects`：待处理 Run + 推荐动作的信息层级；
2. `/runs`：筛选、列表、选中 detail、生命周期轨道、Evidence 主事实；
3. `/studio/*`：三投影宽度、编辑主面、collapsible、Run launch；
4. 任何已有操作、二次确认与 URL 深链保持不变。

### Phase C — Supporting workflows

1. Market / Template detail：trust 与 adoption 层级；
2. Agent：会话、proposal、预算、接管；
3. Cluster / Terminal：事实来源、freshness 和受控命令区；
4. 统一 loading、error、empty、forbidden、stale、success 状态。

### Phase D — Motion, responsive, verification

1. 实施页面与局部状态动效；
2. 完成 reduced-motion、键盘焦点、屏幕阅读器语义；
3. 生成视觉状态：工作台、Run 成功/失败、Studio、Market、Agent、手机端；
4. 执行 typecheck、Vitest、build、Playwright；
5. 在 Windows Chrome/Edge、Ubuntu Chrome、Android Chrome 的字号矩阵中审查 100%/125%/150% 或“大字体”布局。

## 9. 文件影响范围

预期改动：

```text
apps/web/src/App.tsx
apps/web/src/components.tsx
apps/web/src/pages.tsx
apps/web/src/RunTable.tsx
apps/web/src/RunEvidencePanel.tsx
apps/web/src/StudioPage.tsx
apps/web/src/MarketPages.tsx
apps/web/src/AgentPage.tsx
apps/web/src/styles.css  # 迁移后作为 imports / entry
apps/web/src/styles/*    # 新增 tokens、shell、components、pages、motion
tests/ui/visual.spec.js
```

不预期修改：API client 契约、Python service、数据库 schema、Slurm adapter、Worker 状态机。

## 10. 验收标准

- 用户在任何页面 3 秒内能识别环境、当前重点对象与主操作；
- 同屏 L2 anchored work 不超过两个；
- 正文不小于 15px，元数据不小于 12px，交互目标不小于 44px；
- 页面切换无 layout shift，轮询不触发整页入场；
- Windows、Linux、Android 的目标字号矩阵中无截断、重叠、横向页面溢出或不可读对比；
- 既有功能与自动化门禁保持通过；不产生额外 API 请求或 console error。
