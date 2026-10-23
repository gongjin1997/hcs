# frontend/CLAUDE.md · 前端速查

> 详细约定见 `docs/开发文档.md` §4.5、§6。命令在 `frontend/` 下执行：`npm run dev` / `npm run typecheck` / `npm run build`（仓库根目录 `make dev-web`、`make check`）。

## 结构

| 位置 | 作用 |
| --- | --- |
| `src/api/request.ts` | 唯一的 axios 实例：`baseURL=/api/v1`，自动带 `Authorization`（localStorage `hi_token`）与 `X-Tenant-Id`（`hi_tenant`）；`code===0` 时直接返回 `data`，否则弹后端 `message` 并抛 `ApiError`；`{ silent: true }` 不弹窗；401 清登录跳转 |
| `src/api/index.ts` | 按模块封装的接口函数（`get<T>` / `post<T>` …），新接口加在这里 |
| `src/types/index.ts` | 手写的接口类型（与后端 `schemas/` 手工同步，计划改为 OpenAPI 生成，见任务书 T13） |
| `src/utils/dict.ts` / `format.ts` | 状态 / 级别 / 资源类型等中文字典（与后端 `catalog.RESOURCE_TYPES` 同步）/ 时间、数字格式化（dayjs） |
| `src/stores/user.ts` / `app.ts` | 登录信息与权限 `can()` / `canAny()`、租户范围 / 当前租户、侧栏 |
| `src/router/index.ts`、`perms.ts` | 路由 `meta: { title, perm, public }`，守卫按权限点放行；`/m/action/:token` 是邮件快捷操作（公开），`/bigscreen` 大屏 |
| `src/config/menu.ts` | 侧栏菜单（按权限过滤） |
| `src/directives/admin.ts` | `v-perm="'alarm:handle'"`、`v-admin` |
| `src/composables/` | `useTable`（分页表格）、`useAlarmStream`（SSE：换票据 → EventSource → 5/10/20/40/60 秒退避重连） |
| `src/components/` | `EChart`（ECharts 按需注册 `use([...])`）、`PageHeader`、`StatCard`、`StatusTag`、`SeverityTag`、`HealthBadge`、`TenantTag`、`PasswordDialog` |
| `src/views/<模块>/` | 页面；大页面已拆出 Drawer / Dialog / Panel 子组件 |
| `src/styles/` | `tokens.scss`（设计变量）、`index.scss`（全局与 Element Plus 覆盖样式） |

## 约定

- Element Plus 组件直接写在模板里（`unplugin-vue-components` 按需引入）；`ElMessage` / `ElMessageBox` 等函数式 API 需手动 `import { ElMessage } from 'element-plus'`
- EP 样式在 `main.ts` 整体引入一次（为了让 `styles/index.scss` 的覆盖样式始终排在后面），**组件里不要再引入 EP 样式**；不要把 `element-plus/*` 手工并进一个 chunk（会让按需引入失效，见 `vite.config.ts` 注释）
- 页面：页头 `PageHeader`、分页 `useTable`、标签 `StatusTag` / `SeverityTag`、文案字典放 `utils/dict.ts`、时间用 `utils/format.ts`
- 权限：路由 `meta.perm` + 菜单 + 按钮 `v-perm`，脚本里用 `useUserStore().can('xxx')`；权限点以后端 `core/permissions.py` 为准
- 可从邮件 / 总览跳转的页面支持 URL query 预置筛选或打开详情（如告警中心 `?id=`），改路由时保持兼容（`/resources` → `/ledger` 的重定向不要删）
- 业务规则（阈值、保留天数等）从接口读，不在前端硬编码
- TypeScript `strict: true`；不要用 `any` 糊类型，接口类型放 `src/types/`
- 目前没有 ESLint / Prettier / 单元测试（任务书 T12 引入）；在那之前保持与周边代码一致的风格：单引号、无分号、2 空格缩进，不做无关的格式化

## 自检

```bash
npm run typecheck && npm run build     # 必须通过；build 后关注 dist 体积（index ≈ 300 KB / gzip 100 KB，echarts 单独 chunk）
```

页面改动在 `make dev-api` + `make dev-web`（演示数据）下实际操作一遍：管理员和只读角色各看一次（按钮显隐、403 跳转）。