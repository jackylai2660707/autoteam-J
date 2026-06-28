<template>
  <div class="space-y-6">
    <div v-if="showAdminSection" class="glass-card p-5">
      <div class="flex items-center justify-between gap-4 mb-4">
        <div>
          <h2 class="text-lg font-semibold text-white">管理员登录</h2>
          <p class="text-sm text-gray-400 mt-1">
            首次启动先在这里完成管理员登录，系统只保存 Team API 所需的邮箱、session、workspace ID、workspace 名称；账号 OAuth/auth 由 CPA 管理。
          </p>
        </div>
        <span
          class="min-w-[72px] px-3 py-1.5 rounded-full text-xs text-center whitespace-nowrap border"
          :class="adminConfigured
            ? 'bg-green-500/10 text-green-400 border-green-500/20'
            : adminBusy
              ? 'bg-yellow-500/10 text-yellow-300 border-yellow-500/20'
              : 'bg-gray-800 text-gray-400 border-gray-700'"
        >
          {{ adminConfigured ? '已配置' : adminBusy ? '登录中' : '未配置' }}
        </span>
      </div>

      <div v-if="message" class="mb-4 rounded-2xl px-4 py-3 text-sm border" :class="messageClass">
        {{ message }}
      </div>

      <div v-if="adminConfigured && !adminBusy" class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg">
          <div class="text-gray-500 mb-1">管理员邮箱</div>
          <div class="font-mono text-white break-all">{{ props.adminStatus?.email || '-' }}</div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg">
          <div class="text-gray-500 mb-1">Workspace ID</div>
          <div class="font-mono text-white break-all">{{ props.adminStatus?.account_id || '-' }}</div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg md:col-span-2">
          <div class="text-gray-500 mb-1">Workspace 名称</div>
          <div class="text-white">{{ props.adminStatus?.workspace_name || '未识别' }}</div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg md:col-span-2">
          <div class="text-gray-500 mb-1">Session Token</div>
          <div v-if="props.adminStatus?.session_present" class="text-green-400 text-xs">已配置</div>
          <div v-else class="space-y-2">
            <div class="text-amber-400 text-xs">未配置（Team 管理功能需要 session token）</div>
            <div class="text-gray-400 text-xs space-y-2">
              <div>获取方式：</div>
              <ol class="list-decimal list-inside space-y-1">
                <li>
                  在浏览器中打开
                  <a href="https://chatgpt.com" target="_blank" rel="noreferrer" class="text-blue-400 hover:underline">
                    chatgpt.com
                  </a>
                  并登录管理员账号
                </li>
                <li>按 F12 打开开发者工具 → Application → Cookies → chatgpt.com</li>
                <li>找到 <code class="bg-gray-800 px-1 rounded">__Secure-next-auth.session-token</code>，也可以直接复制整段 Cookie header 或 DevTools 导出的 cookies JSON</li>
                <li>
                  如果有 <code class="bg-gray-800 px-1 rounded">.0</code> 和
                  <code class="bg-gray-800 px-1 rounded">.1</code> 分片，系统会自动按顺序拼接
                </li>
                <li>粘贴到下方输入框，系统只保存解析出的 session_token，不保存整段 Cookie</li>
              </ol>
            </div>
            <div class="space-y-2">
              <input
                v-model.trim="sessionToken"
                type="password"
                placeholder="可粘贴纯 token / Cookie header / cookies JSON"
                class="w-full px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-xs text-white font-mono focus:outline-none focus:border-blue-500"
              />
              <div class="flex justify-end">
                <button
                  @click="importSessionToken"
                  :disabled="submitting || !sessionEmail || !sessionToken"
                  class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs rounded transition disabled:opacity-50"
                >
                  {{ submitting ? '校验中...' : '保存' }}
                </button>
              </div>
            </div>
          </div>
        </div>
        <div class="px-3 py-3 bg-gray-800/60 border border-gray-800 rounded-lg md:col-span-2">
          <div class="text-gray-500 mb-1">管理员密码</div>
          <div class="text-white">{{ props.adminStatus?.password_saved ? '已保存（仅管理员登录流程历史状态）' : '未保存' }}</div>
        </div>
      </div>

      <div v-if="!adminBusy" class="mt-4">
        <div v-if="!adminConfigured" class="space-y-4">
          <div class="flex flex-col sm:flex-row gap-3">
            <input
              v-model.trim="email"
              type="email"
              autocomplete="username"
              placeholder="输入主号邮箱"
              class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
            />
            <button
              @click="startLogin"
              :disabled="submitting || !email"
              class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50"
            >
              {{ submitting ? '提交中...' : '开始登录' }}
            </button>
          </div>

          <div class="border border-gray-800 rounded-xl p-4 bg-gray-800/30">
            <div class="text-sm font-medium text-white">或手动导入 session_token</div>
            <p class="text-xs text-gray-400 mt-1 mb-3">
              适合你已经在浏览器里拿到 ChatGPT 登录 Cookie 的场景。可以粘贴纯 token、整段 Cookie header，或 DevTools/插件导出的 cookies JSON；系统会提取 <span class="font-mono">__Secure-next-auth.session-token</span> 并校验 workspace。
            </p>
            <div class="mb-3 rounded-xl border border-cyan-500/20 bg-cyan-500/10 px-3 py-2 text-xs leading-5 text-cyan-100">
              示例格式：<span class="font-mono">__Secure-next-auth.session-token=...</span> 或
              <span class="font-mono">Cookie: __Secure-next-auth.session-token.0=...; __Secure-next-auth.session-token.1=...</span>。
              不需要粘贴 access token/API key。
            </div>
            <div class="text-gray-400 text-xs space-y-2 mb-3">
              <div>获取方式：</div>
              <ol class="list-decimal list-inside space-y-1">
                <li>
                  在浏览器中打开
                  <a href="https://chatgpt.com" target="_blank" rel="noreferrer" class="text-blue-400 hover:underline">
                    chatgpt.com
                  </a>
                  并登录管理员账号
                </li>
                <li>按 F12 打开开发者工具 → Application → Cookies → chatgpt.com</li>
                <li>找到 <code class="bg-gray-800 px-1 rounded">__Secure-next-auth.session-token</code>，或复制包含它的整段 Cookie/JSON</li>
                <li>
                  如果有 <code class="bg-gray-800 px-1 rounded">.0</code> 和
                  <code class="bg-gray-800 px-1 rounded">.1</code> 分片，不需要手动拼接，后端会自动处理
                </li>
                <li>粘贴到下方输入框</li>
              </ol>
            </div>
            <div class="space-y-3">
              <input
                v-model.trim="sessionEmail"
                type="email"
                autocomplete="username"
                placeholder="输入主号邮箱"
                class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-cyan-500"
              />
              <textarea
                v-model.trim="sessionToken"
                rows="4"
                spellcheck="false"
                placeholder="粘贴纯 session_token，或整段 Cookie header / DevTools cookies JSON"
                class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white font-mono focus:outline-none focus:border-cyan-500"
              ></textarea>
              <div class="flex justify-end">
                <button
                  @click="importSessionToken"
                  :disabled="submitting || !sessionEmail || !sessionToken"
                  class="px-4 py-2 bg-cyan-700 hover:bg-cyan-600 text-white text-sm rounded-lg transition disabled:opacity-50"
                >
                  {{ submitting ? '校验中...' : '导入 session_token' }}
                </button>
              </div>
            </div>
          </div>
        </div>

        <div v-else class="space-y-3">
          <div class="rounded-xl border border-blue-500/20 bg-blue-500/10 px-4 py-3 text-sm text-blue-200">
            主号 Codex OAuth/auth 由 CPA 管理；AutoTeam 不再登录、同步或删除主号 OAuth。swap_seat 会强制母号/admin 使用 Codex seat。
          </div>
          <button
            @click="logoutAdmin"
            :disabled="submitting"
            class="px-4 py-2 bg-rose-700/80 hover:bg-rose-700 text-white text-sm rounded-lg transition disabled:opacity-50"
          >
            {{ submitting ? '处理中...' : '清除登录态' }}
          </button>
        </div>
      </div>

      <div v-if="adminBusy" class="space-y-4">
        <div class="text-sm text-gray-300">
          当前邮箱: <span class="font-mono">{{ loginEmail || props.adminStatus?.email || '-' }}</span>
        </div>

        <div v-if="props.adminStatus?.login_step === 'password_required'" class="flex flex-col sm:flex-row gap-3">
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            placeholder="输入主号密码"
            :disabled="submitting"
            class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          />
          <button
            @click="submitPassword"
            :disabled="submitting || !password"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50"
          >
            {{ submitting ? '提交中...' : '提交密码' }}
          </button>
        </div>

        <div v-else-if="props.adminStatus?.login_step === 'code_required'" class="flex flex-col sm:flex-row gap-3">
          <input
            v-model.trim="code"
            type="text"
            inputmode="numeric"
            autocomplete="one-time-code"
            placeholder="输入邮箱验证码"
            :disabled="submitting"
            class="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          />
          <button
            @click="submitCode"
            :disabled="submitting || !code"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50 disabled:bg-gray-700 disabled:hover:bg-gray-700"
          >
            {{ submitting ? '提交中...' : '提交验证码' }}
          </button>
        </div>

        <div v-else-if="props.adminStatus?.login_step === 'workspace_required'" class="space-y-3">
          <div class="text-sm text-gray-300">
            请选择要进入的组织 / workspace
          </div>
          <select
            v-model="workspaceOptionId"
            :disabled="submitting"
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500"
          >
            <option disabled value="">请选择组织</option>
            <option
              v-for="opt in props.adminStatus?.workspace_options || []"
              :key="opt.id"
              :value="opt.id"
            >
              {{ opt.label }}{{ opt.kind === 'fallback' ? ' (可能是个人/免费)' : '' }}
            </option>
          </select>
          <button
            @click="submitWorkspace"
            :disabled="submitting || !workspaceOptionId"
            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50 disabled:bg-gray-700 disabled:hover:bg-gray-700"
          >
            {{ submitting ? '提交中...' : '确认组织选择' }}
          </button>
        </div>

        <div v-if="submitting && adminSubmittingHint" class="text-xs text-blue-300">
          {{ adminSubmittingHint }}
        </div>

        <div class="flex justify-end">
          <button
            @click="cancelLogin"
            :disabled="submitting"
            class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 rounded-lg border border-gray-700 transition disabled:opacity-50"
          >
            取消登录
          </button>
        </div>
      </div>

    </div>

    <div v-if="showAutoCheckSection" class="glass-card p-5">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-lg font-semibold text-white">巡检设置</h2>
        <span v-if="saved" class="text-xs text-green-400 transition">已保存</span>
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div>
          <label class="block text-sm text-gray-400 mb-1">巡检间隔</label>
          <div class="flex items-center gap-2">
            <input v-model.number="form.interval" type="number" min="1"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
            <span class="text-sm text-gray-500 shrink-0">分钟</span>
          </div>
        </div>
        <div>
          <label class="block text-sm text-gray-400 mb-1">ChatGPT/OAuth active 保留数</label>
          <div class="flex items-center gap-2">
            <input v-model.number="form.target_seats" type="number" min="1" max="5"
              class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
            <span class="text-sm text-gray-500 shrink-0">个</span>
          </div>
        </div>
      </div>

      <label class="mt-4 flex cursor-pointer items-start gap-3 rounded-xl border border-gray-800 bg-gray-800/40 px-4 py-3">
        <input
          v-model="form.replace_with_pending_invite"
          type="checkbox"
          class="mt-1 h-4 w-4 rounded border-gray-700 bg-gray-900 text-blue-600 focus:ring-blue-500"
        />
        <span class="text-sm">
          <span class="block font-medium text-white">GPT seat 低于目标时，自动补位</span>
          <span class="mt-1 block text-xs leading-5 text-gray-400">
            自动巡检会先运行 swap_seat；如果 swap 后有效 GPT seat 少于目标保留数，就按下方模式补位。注册前只读检查，注册后只激活新号；不会 kick/remove/cancel invite。
          </span>
        </span>
      </label>

      <div
        v-if="form.replace_with_pending_invite"
        class="mt-4 grid grid-cols-1 gap-3 rounded-xl border border-cyan-500/20 bg-cyan-500/5 px-4 py-3 sm:grid-cols-[220px_1fr]"
      >
        <div>
          <label class="block text-sm font-medium text-cyan-100 mb-1">自动补位模式</label>
          <select
            v-model="form.replace_mode"
            class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-cyan-500"
          >
            <option value="pending_invite">消费已有 pending invite</option>
            <option value="create_invite">创建新 CFMail invite</option>
          </select>
        </div>
        <p class="text-xs leading-5 text-cyan-100/75">
          pending 模式只使用 Team 里已经存在的 CFMail invite；create 模式会创建随机 CFMail 地址并真实发送一个新的 Team invite。两种模式都只在 GPT seat 低于目标时触发，并继续遵守 active 保留数。
        </p>
      </div>

      <div class="mt-3 flex items-start justify-between gap-3">
        <p class="text-xs leading-5 text-gray-500">
          每 {{ form.interval }} 分钟触发一次：
          <span v-if="form.replace_with_pending_invite">
            自动检测 → 必要时{{ form.replace_mode === 'create_invite' ? '创建 invite 补位' : 'pending invite 补位' }}
          </span>
          <span v-else>swap_seat 收敛</span>。
          若配置了多 Team，会逐个 Team 独立执行同一策略。通过 CPA 检查所有 Team member 的 5h + weekly quota，保留 {{ form.target_seats }} 个 ChatGPT seat/OAuth active（允许 1~5）；母号/admin 和其余非白名单成员全部 Codex；白名单不查 quota、不切 seat、不改 CPA OAuth。
        </p>
        <button @click="save" :disabled="saving"
          class="shrink-0 px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:opacity-50">
          {{ saving ? '保存中...' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch, onMounted } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  adminStatus: {
    type: Object,
    default: null,
  },
  codexStatus: {
    type: Object,
    default: null,
  },
  section: {
    type: String,
    default: 'all',
  },
})

const emit = defineEmits(['refresh', 'admin-progress'])

const form = ref({
  interval: 5,
  target_seats: 2,
  replace_with_pending_invite: false,
  replace_mode: 'pending_invite',
})
const saving = ref(false)
const saved = ref(false)

const email = ref('')
const sessionEmail = ref('')
const sessionToken = ref('')
const password = ref('')
const code = ref('')
const workspaceOptionId = ref('')
const loginEmail = ref('')
const submitting = ref(false)
const message = ref('')
const messageClass = ref('')
const adminSubmittingHint = ref('')

const adminConfigured = computed(() => !!props.adminStatus?.configured)
const adminBusy = computed(() => !!props.adminStatus?.login_in_progress)
const showAdminSection = computed(() => props.section !== 'auto-check')
const showAutoCheckSection = computed(() => props.section !== 'admin')

watch(
  () => props.adminStatus,
  (next) => {
    if (next?.configured && next.email) {
      email.value = next.email
      sessionEmail.value = next.email
    }
    if (!next?.login_in_progress) {
      password.value = ''
      code.value = ''
      workspaceOptionId.value = ''
      adminSubmittingHint.value = ''
      loginEmail.value = next?.email || loginEmail.value
    }
    if (next?.login_step === 'workspace_required' && !workspaceOptionId.value) {
      const preferred = next?.workspace_options?.find(opt => opt.kind === 'preferred')
      workspaceOptionId.value = preferred?.id || next?.workspace_options?.[0]?.id || ''
    }
  },
  { immediate: true },
)

onMounted(async () => {
  if (showAutoCheckSection.value) {
    await loadAutoCheckConfig()
  }
})

function setMessage(text, type = 'success') {
  message.value = text
  messageClass.value = type === 'success'
    ? 'bg-green-500/10 text-green-400 border-green-500/20'
    : 'bg-red-500/10 text-red-400 border-red-500/20'
  window.clearTimeout(setMessage._timer)
  setMessage._timer = window.setTimeout(() => {
    message.value = ''
  }, 8000)
}

async function loadAutoCheckConfig() {
  try {
    const cfg = await api.getAutoCheckConfig()
    form.value = {
      interval: Math.round(cfg.interval / 60),
      target_seats: clampActiveLimit(cfg.target_seats ?? 2),
      replace_with_pending_invite: !!cfg.replace_with_pending_invite,
      replace_mode: normalizeReplaceMode(cfg.replace_mode),
    }
  } catch (e) {
    console.error('加载巡检配置失败:', e)
  }
}

async function startLogin() {
  submitting.value = true
  adminSubmittingHint.value = '正在打开管理员登录页...'
  try {
    loginEmail.value = email.value
    const result = await api.startAdminLogin(email.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '已进入下一步登录流程')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function importSessionToken() {
  submitting.value = true
  adminSubmittingHint.value = '正在校验 session_token 并识别 workspace...'
  try {
    loginEmail.value = sessionEmail.value
    const result = await api.submitAdminSession(sessionEmail.value, sessionToken.value)
    sessionToken.value = ''
    setMessage(result.status === 'completed' ? 'session_token 导入成功' : 'session_token 已提交')
    emit('refresh')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function submitPassword() {
  submitting.value = true
  adminSubmittingHint.value = '密码已提交，正在等待登录页响应...'
  try {
    const result = await api.submitAdminPassword(password.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '密码已提交，请继续下一步')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function submitCode() {
  submitting.value = true
  adminSubmittingHint.value = '验证码已提交，正在等待登录页响应，通常需要 5 到 10 秒...'
  try {
    const result = await api.submitAdminCode(code.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '验证码已提交，请继续下一步')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function submitWorkspace() {
  submitting.value = true
  adminSubmittingHint.value = '组织选择已提交，正在等待登录页响应...'
  try {
    const result = await api.submitAdminWorkspace(workspaceOptionId.value)
    setMessage(result.status === 'completed' ? '管理员登录完成' : '组织选择已提交，请继续下一步')
    emit('admin-progress')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
    adminSubmittingHint.value = ''
  }
}

async function cancelLogin() {
  submitting.value = true
  try {
    await api.cancelAdminLogin()
    password.value = ''
    code.value = ''
    setMessage('管理员登录已取消')
    emit('refresh')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
  }
}

async function logoutAdmin() {
  submitting.value = true
  try {
    await api.logoutAdmin()
    password.value = ''
    code.value = ''
    setMessage('管理员登录态已清除')
    emit('refresh')
  } catch (e) {
    setMessage(e.message, 'error')
  } finally {
    submitting.value = false
  }
}

async function save() {
  saving.value = true
  saved.value = false
  try {
    const cfg = await api.setAutoCheckConfig({
      interval: form.value.interval * 60,
      target_seats: clampActiveLimit(form.value.target_seats),
      replace_with_pending_invite: !!form.value.replace_with_pending_invite,
      replace_mode: normalizeReplaceMode(form.value.replace_mode),
    })
    form.value = {
      interval: Math.round(cfg.interval / 60),
      target_seats: clampActiveLimit(cfg.target_seats ?? 2),
      replace_with_pending_invite: !!cfg.replace_with_pending_invite,
      replace_mode: normalizeReplaceMode(cfg.replace_mode),
    }
    saved.value = true
    setTimeout(() => { saved.value = false }, 3000)
  } catch (e) {
    console.error('保存失败:', e)
  } finally {
    saving.value = false
  }
}

function clampActiveLimit(value) {
  const n = Number.parseInt(value, 10)
  if (Number.isNaN(n)) return 2
  return Math.max(1, Math.min(5, n))
}

function normalizeReplaceMode(value) {
  return String(value || '').trim().toLowerCase().replace('-', '_') === 'create_invite' ? 'create_invite' : 'pending_invite'
}
</script>
