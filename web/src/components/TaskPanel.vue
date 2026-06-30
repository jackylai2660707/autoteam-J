<template>
  <div class="mt-6 bg-gray-900 border border-gray-800 rounded-xl p-4">
    <h2 class="text-lg font-semibold text-white mb-4">{{ panelTitle }}</h2>
    <div v-if="showAdminHint" class="mb-4 px-4 py-3 rounded-lg text-sm border bg-amber-500/10 text-amber-300 border-amber-500/20">
      {{ adminHint }}
    </div>
    <div v-if="mode === 'pool'" class="mb-4 px-4 py-3 rounded-lg text-sm border bg-blue-500/10 text-blue-300 border-blue-500/20">
      冷却限制：每天最多执行 3 次 swap_seat；每次 swap_seat 至少间隔 2 小时。冷却由后端强制执行，不能绕过。
      自动补位只会在 swap 后 GPT seat 低于目标保留数时触发；pending 模式消费已有 invite，create 模式会显式创建一个随机 CFMail invite。两种模式都会继续遵守 ChatGPT/OAuth active 上限。
    </div>
    <div v-if="mode === 'pool'" class="mb-4 flex flex-col gap-2 rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div class="text-sm font-medium text-white">操作目标</div>
        <div class="mt-1 text-xs text-gray-500">
          swap_seat / 自动检测会作用于这里选择的 Team；多 Team 模式下必须显式选择，避免误操作默认管理员 Team。「多 Team 自动调度」会忽略该选择并调度全部 enabled Team。
        </div>
      </div>
      <select
        v-model="selectedAccountId"
        class="min-w-[260px] rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
      >
        <option value="">{{ teamPlaceholder }}</option>
        <option
          v-for="team in selectableTeams"
          :key="team.account_id || team.id"
          :value="team.account_id"
          :disabled="team.enabled === false"
        >
          {{ team.label || team.workspace_name || team.account_id }}{{ team.enabled === false ? '（disabled）' : '' }}
        </option>
      </select>
    </div>
    <div
      v-if="selectedTeam"
      class="mb-4 flex flex-wrap gap-2 rounded-xl border border-blue-500/20 bg-blue-500/5 px-4 py-3 text-xs text-blue-100"
    >
      <span class="font-medium text-white">当前目标：{{ selectedTeam.label || selectedTeam.workspace_name || selectedTeam.account_id }}</span>
      <span class="rounded bg-gray-900/70 px-2 py-0.5">active {{ selectedTeam.max_chatgpt_active || 2 }}</span>
      <span class="rounded bg-gray-900/70 px-2 py-0.5">{{ selectedTeam.session_present ? '独立 session' : '共享默认 session' }}</span>
      <span v-if="selectedTeam.pending_invite_email" class="rounded bg-amber-500/10 px-2 py-0.5 text-amber-200">
        pending {{ selectedTeam.pending_invite_email }}
      </span>
      <span
        v-if="selectedTeam.invite_domains"
        class="break-all rounded bg-emerald-500/10 px-2 py-0.5 font-mono text-emerald-200"
      >
        invite pool {{ selectedTeam.invite_domains }}
      </span>
    </div>
    <div
      v-if="showTeamSelectionHint"
      class="mb-4 px-4 py-3 rounded-lg text-sm border bg-amber-500/10 text-amber-300 border-amber-500/20"
    >
      当前配置了多个 Team。请先选择目标 Team 后再执行单 Team 的 swap_seat / 自动检测替换；如果要巡检全部 Team，请直接使用「多 Team 自动调度」。
    </div>
    <div class="flex flex-wrap gap-3">
      <button v-for="action in visibleActions" :key="action.key"
        @click="execute(action)"
        :disabled="isDisabled(action)"
        class="px-4 py-2 rounded-lg text-sm font-medium transition border"
        :class="isDisabled(action)
          ? 'bg-gray-800 text-gray-500 border-gray-700 cursor-not-allowed'
          : `${action.style} hover:opacity-80`">
        {{ action.label }}
      </button>
    </div>

    <!-- 参数输入 -->
    <div v-if="showParams" class="mt-4 rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3">
      <div class="flex flex-wrap items-center gap-3">
        <label class="text-sm text-gray-400">{{ paramLabel }}:</label>
        <input v-model.number="paramValue" type="number" min="1" :max="paramMax"
          class="w-20 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
        <button @click="confirmAction" :disabled="!canSubmitPendingAction"
          class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition disabled:cursor-not-allowed disabled:opacity-50">
          确认执行
        </button>
        <button @click="cancelParams"
          class="px-3 py-1.5 text-gray-400 hover:text-white text-sm transition">
          取消
        </button>
      </div>
      <div
        v-if="pendingAction?.key === 'invite-add'"
        class="mt-3 rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-3 text-xs text-emerald-100"
      >
        <label class="mb-2 block text-sm font-medium text-emerald-50">CFMail 域名池（可选）</label>
        <textarea
          v-model.trim="inviteDomainsInput"
          rows="3"
          spellcheck="false"
          placeholder="例如：pool-a.example.com; *.pool-b.example.com"
          class="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 font-mono text-xs text-white focus:border-emerald-500 focus:outline-none"
        ></textarea>
        <div v-if="inviteDomainError" class="mt-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-red-200">
          {{ inviteDomainError }}
        </div>
        <div v-else-if="inviteDomainOptions.length" class="mt-3 flex flex-wrap gap-2">
          <span
            v-for="(option, index) in inviteDomainOptions"
            :key="`${option.domain}-${index}`"
            class="rounded bg-gray-900/70 px-2 py-1 font-mono text-[11px] text-emerald-100"
          >
            #{{ index + 1 }} {{ option.enable_random_subdomain ? '*.' : '' }}{{ option.domain }}
          </span>
        </div>
        <div v-else class="mt-2 rounded-lg border border-gray-700/70 bg-gray-900/70 px-3 py-2 text-gray-300">
          将使用 Team 配置的域名池；若 Team 未配置，则使用 CFMail 默认域名。
        </div>
        <label class="mt-3 flex items-start gap-3 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs leading-5 text-red-100">
          <input
            v-model="confirmCreateInvite"
            type="checkbox"
            class="mt-0.5 rounded border-red-400/40 bg-gray-900 text-red-500 focus:ring-red-500"
          />
          <span>
            <span class="font-medium text-red-50">确认发送新的 Team invite</span>
            <span class="block text-red-200/80">
              该任务会创建 CFMail 地址、发送真实邀请，并继续注册与 PAT 上传。
            </span>
          </span>
        </label>
      </div>
      <div
        v-if="pendingAction?.key === 'bulk-invite'"
        class="mt-3 rounded-lg border border-orange-500/20 bg-orange-500/10 px-3 py-3 text-xs text-orange-100"
      >
        <label class="mb-2 block text-sm font-medium text-orange-50">批量 invite 邮箱列表（默认 Codex seat）</label>
        <textarea
          v-model.trim="bulkInviteEmailsInput"
          rows="6"
          spellcheck="false"
          placeholder="每行一个邮箱，也支持逗号/空格分隔&#10;user1@example.com&#10;user2@example.com"
          class="w-full rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 font-mono text-xs text-white focus:border-orange-500 focus:outline-none"
        ></textarea>
        <div v-if="bulkInviteError" class="mt-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-red-200">
          {{ bulkInviteError }}
        </div>
        <div v-else class="mt-2 rounded-lg border border-gray-700/70 bg-gray-900/70 px-3 py-2 text-gray-200">
          将发送 {{ bulkInviteEmails.length }} 个 Codex seat invite；后端按 5 个/批、并发 {{ paramValue || 3 }} 批执行。这里只发送邀请，不注册账号、不上传 PAT。
        </div>
        <label class="mt-3 flex items-start gap-3 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs leading-5 text-red-100">
          <input
            v-model="confirmBulkInvite"
            type="checkbox"
            class="mt-0.5 rounded border-red-400/40 bg-gray-900 text-red-500 focus:ring-red-500"
          />
          <span>
            <span class="font-medium text-red-50">确认批量发送真实 Team invite</span>
            <span class="block text-red-200/80">
              会向列表中的邮箱发送 Codex seat 邀请邮件；不会自动消费这些 invite。
            </span>
          </span>
        </label>
      </div>
      <label
        v-if="pendingAction?.key === 'clear-pending-invites'"
        class="mt-3 flex items-start gap-3 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs leading-5 text-red-100"
      >
        <input
          v-model="confirmClearPendingInvites"
          type="checkbox"
          class="mt-0.5 rounded border-red-400/40 bg-gray-900 text-red-500 focus:ring-red-500"
        />
        <span>
          <span class="font-medium text-red-50">确认清空当前目标 Team 的所有 pending invite</span>
          <span class="block text-red-200/80">
            只取消未接受的邀请，不会移除 Team member，也不会删除 CPA auth。后端会先读取 pending 列表，再逐个按 email 删除。
          </span>
        </span>
      </label>
      <label
        v-if="pendingAction?.key === 'manage-teams'"
        class="mt-3 flex items-start gap-3 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs leading-5 text-amber-100"
      >
        <input
          v-model="replaceWithPendingInvite"
          type="checkbox"
          class="mt-0.5 rounded border-amber-400/40 bg-gray-900 text-amber-500 focus:ring-amber-500"
        />
        <span>
          <span class="font-medium text-amber-50">GPT seat 低于目标时自动补位</span>
          <span class="block text-amber-200/80">
            开启后，每个 Team 在 swap 后有效 GPT seat 少于目标时才会补位；关闭则只做 swap_seat 收敛。
          </span>
        </span>
      </label>
      <div
        v-if="pendingAction?.key === 'manage-teams' && replaceWithPendingInvite"
        class="mt-3 flex flex-wrap items-center gap-3 rounded-lg border border-cyan-500/20 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-100"
      >
        <span class="font-medium">补位模式</span>
        <select
          v-model="replaceMode"
          class="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-white focus:border-cyan-500 focus:outline-none"
        >
          <option value="">使用巡检配置</option>
          <option value="pending_invite">消费已有 pending invite</option>
          <option value="create_invite">创建新 CFMail invite</option>
        </select>
      </div>
    </div>

    <!-- 结果提示 -->
    <div v-if="message" class="mt-4 px-4 py-3 rounded-lg text-sm" :class="messageClass">
      {{ message }}
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  runningTask: Object,
  adminStatus: {
    type: Object,
    default: null,
  },
  mode: {
    type: String,
    default: 'all',
  },
  teams: {
    type: Array,
    default: () => [],
  },
  targetAccountId: {
    type: String,
    default: '',
  },
})
const emit = defineEmits(['task-started', 'refresh', 'update:targetAccountId'])

const actions = [
  { key: 'swap-seats', group: 'pool', label: '执行 swap_seat', method: 'startSwapSeats', needParam: true, paramName: 'max_chatgpt_active', style: 'bg-blue-600 text-white border-blue-500' },
  { key: 'auto-detect-replace', group: 'pool', label: '目标 Team 自动检测并替换', method: 'startAutoDetectReplace', needParam: false, style: 'bg-cyan-600 text-white border-cyan-500' },
  { key: 'invite-add', group: 'pool', label: '新增 invite 注册', method: 'startInviteAdd', needParam: true, paramName: 'max_chatgpt_active', style: 'bg-emerald-600 text-white border-emerald-500' },
  { key: 'bulk-invite', group: 'pool', label: '批量发送 invite', method: 'startBulkInvite', needParam: true, paramName: 'concurrency', style: 'bg-orange-600 text-white border-orange-500' },
  { key: 'clear-pending-invites', group: 'pool', label: '清空 pending invite', method: 'startClearPendingInvites', needParam: true, paramName: 'concurrency', style: 'bg-rose-600 text-white border-rose-500' },
  { key: 'manage-teams', group: 'pool', label: '多 Team 自动调度', method: 'startManageTeams', needParam: true, paramName: 'max_chatgpt_active', style: 'bg-indigo-600 text-white border-indigo-500' },
]

const showParams = ref(false)
const paramLabel = ref('')
const paramValue = ref(5)
const pendingAction = ref(null)
const message = ref('')
const messageClass = ref('')
const selectedAccountId = ref('')
const replaceWithPendingInvite = ref(true)
const replaceMode = ref('')
const inviteDomainsInput = ref('')
const confirmCreateInvite = ref(false)
const bulkInviteEmailsInput = ref('')
const confirmBulkInvite = ref(false)
const confirmClearPendingInvites = ref(false)
const adminReady = computed(() => !!props.adminStatus?.configured)
const selectableTeams = computed(() => {
  const teams = Array.isArray(props.teams) ? props.teams : []
  return teams.filter(team => team?.enabled !== false)
})
const selectedTeam = computed(() => selectableTeams.value.find(team => team.account_id === selectedAccountId.value) || null)
const multiTeamMode = computed(() => props.mode === 'pool' && selectableTeams.value.length > 1)
const teamPlaceholder = computed(() => multiTeamMode.value ? '请选择 Team' : '默认管理员 Team')
const showTeamSelectionHint = computed(() => multiTeamMode.value && !selectedAccountId.value)
const visibleActions = computed(() => {
  if (props.mode === 'all') return actions
  return actions.filter(action => action.group === props.mode)
})
const panelTitle = computed(() => {
  if (props.mode === 'pool') return 'Team 操作'
  return '操作'
})
const paramMax = computed(() => pendingAction.value?.paramName === 'max_chatgpt_active' ? 5 : 8)
const adminHint = computed(() => {
  return '请先在「配置面板」页完成管理员登录；swap_seat 只会切换 seat 和 CPA OAuth active/disabled，不会 kick Team 成员。'
})
const showAdminHint = computed(() => !adminReady.value && props.mode === 'pool')
const inviteDomainOptions = computed(() => parseInviteDomainOptions(inviteDomainsInput.value))
const inviteDomainError = computed(() => {
  const raw = String(inviteDomainsInput.value || '').trim()
  if (!raw) return ''
  return inviteDomainOptions.value.length ? '' : '请填写有效域名，例如 example.com、*.example.com 或 xxxxx.example.com'
})
const bulkInviteEmails = computed(() => parseEmailList(bulkInviteEmailsInput.value).emails)
const bulkInviteInvalidEmails = computed(() => parseEmailList(bulkInviteEmailsInput.value).invalid)
const bulkInviteError = computed(() => {
  if (pendingAction.value?.key !== 'bulk-invite') return ''
  if (!String(bulkInviteEmailsInput.value || '').trim()) return '请填写至少 1 个邮箱'
  if (bulkInviteInvalidEmails.value.length) return `以下邮箱格式无效：${bulkInviteInvalidEmails.value.slice(0, 3).join(', ')}`
  return bulkInviteEmails.value.length ? '' : '请填写至少 1 个有效邮箱'
})
const canSubmitPendingAction = computed(() => {
  const action = pendingAction.value
  if (!action || isDisabled(action)) return false
  if (action.key === 'invite-add') {
    return confirmCreateInvite.value && !inviteDomainError.value
  }
  if (action.key === 'bulk-invite') {
    return confirmBulkInvite.value && !bulkInviteError.value
  }
  if (action.key === 'clear-pending-invites') {
    return confirmClearPendingInvites.value
  }
  return true
})

watch(
  selectableTeams,
  (teams) => {
    if (teams.length === 1 && teams[0]?.account_id) {
      selectedAccountId.value = teams[0].account_id
    }
    if (selectedAccountId.value && !teams.some(team => team.account_id === selectedAccountId.value)) {
      selectedAccountId.value = ''
    }
  },
  { immediate: true },
)

watch(
  () => props.targetAccountId,
  (value) => {
    const next = String(value || '').trim()
    if (next !== selectedAccountId.value) {
      selectedAccountId.value = next
    }
  },
  { immediate: true },
)

watch(
  selectedAccountId,
  (value) => {
    if (value !== props.targetAccountId) {
      emit('update:targetAccountId', value)
    }
  },
)

function requiresExplicitTeam(action) {
  return multiTeamMode.value && ['swap-seats', 'auto-detect-replace', 'invite-add', 'bulk-invite', 'clear-pending-invites'].includes(action.key)
}

function isDisabled(action) {
  if (props.runningTask) return true
  if (!adminReady.value && !action.allowWithoutAdmin) return true
  if (requiresExplicitTeam(action) && !selectedAccountId.value) return true
  return false
}

function parseInviteDomainOptions(value) {
  return String(value || '')
    .split(/[;,\n]+/)
    .map(part => part.trim().toLowerCase().replace(/^@+/, ''))
    .filter(Boolean)
    .map((item) => {
      let enableRandomSubdomain = false
      let domain = item
      if (domain.startsWith('*.')) {
        enableRandomSubdomain = true
        domain = domain.slice(2)
      } else if (domain.startsWith('{random}.')) {
        enableRandomSubdomain = true
        domain = domain.slice('{random}.'.length)
      } else {
        const match = domain.match(/^x{3,}\.(.+)$/i)
        if (match) {
          enableRandomSubdomain = true
          domain = match[1]
        }
      }
      domain = domain.replace(/^\.+|\.+$/g, '')
      if (!/^[a-z0-9.-]+\.[a-z0-9-]+$/.test(domain)) return null
      return {
        domain,
        enable_random_subdomain: enableRandomSubdomain,
      }
    })
    .filter(Boolean)
}

function parseEmailList(value) {
  const seen = new Set()
  const emails = []
  const invalid = []
  for (const part of String(value || '').split(/[,;\s]+/)) {
    const email = part.trim().toLowerCase()
    if (!email) continue
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      invalid.push(email)
      continue
    }
    if (seen.has(email)) continue
    seen.add(email)
    emails.push(email)
  }
  return { emails, invalid }
}

async function execute(action) {
  if (isDisabled(action)) return
  message.value = ''
  if (action.needParam) {
    pendingAction.value = action
    replaceWithPendingInvite.value = true
    replaceMode.value = ''
    confirmCreateInvite.value = false
    confirmBulkInvite.value = false
    confirmClearPendingInvites.value = false
    bulkInviteEmailsInput.value = ''
    inviteDomainsInput.value = action.key === 'invite-add'
      ? String(selectedTeam.value?.invite_domains || '').trim()
      : ''
    paramLabel.value = action.key === 'manage-teams'
      ? '默认 ChatGPT/OAuth active 保留数（1~5；Team 配置优先）'
      : action.key === 'invite-add'
        ? '注册后 ChatGPT/OAuth active 保留数（1~5）'
      : action.key === 'bulk-invite'
        ? '并发批次数（1~8）'
      : action.key === 'clear-pending-invites'
        ? '并发删除数（1~8）'
      : action.paramName === 'max_chatgpt_active'
        ? 'ChatGPT/OAuth active 保留数（1~5）'
        : '目标成员数'
    paramValue.value = action.paramName === 'max_chatgpt_active'
      ? Number(action.key === 'manage-teams' ? 2 : (selectedTeam.value?.max_chatgpt_active || 2))
      : action.key === 'clear-pending-invites'
        ? 4
        : 3
    showParams.value = true
    return
  }
  await doExecute(action)
}

async function confirmAction() {
  if (!canSubmitPendingAction.value) return
  showParams.value = false
  if (pendingAction.value) {
    await doExecute(pendingAction.value, paramValue.value)
    pendingAction.value = null
  }
}

function cancelParams() {
  showParams.value = false
  pendingAction.value = null
  confirmCreateInvite.value = false
  confirmBulkInvite.value = false
  confirmClearPendingInvites.value = false
  inviteDomainsInput.value = ''
  bulkInviteEmailsInput.value = ''
}

async function doExecute(action, param) {
  try {
    const normalizedParam = action.paramName === 'max_chatgpt_active'
      ? Math.max(1, Math.min(5, Number.parseInt(param, 10) || 2))
      : param
    let result
    if (action.key === 'swap-seats') {
      result = await api.startSwapSeats(normalizedParam, selectedAccountId.value)
    } else if (action.key === 'auto-detect-replace') {
      result = await api.startAutoDetectReplace('', selectedAccountId.value)
    } else if (action.key === 'invite-add') {
      result = await api.startInviteAdd(
        selectedAccountId.value,
        normalizedParam,
        true,
        inviteDomainsInput.value,
      )
    } else if (action.key === 'bulk-invite') {
      result = await api.startBulkInvite(
        selectedAccountId.value,
        bulkInviteEmails.value.join('\n'),
        normalizedParam,
        true,
      )
    } else if (action.key === 'clear-pending-invites') {
      result = await api.startClearPendingInvites(selectedAccountId.value, normalizedParam, true)
    } else if (action.key === 'manage-teams') {
      result = await api.startManageTeams(normalizedParam, replaceWithPendingInvite.value, replaceMode.value)
    } else {
      result = await api[action.method](normalizedParam)
    }
    message.value = `任务已提交: ${result.task_id}`
    messageClass.value = 'bg-blue-500/10 text-blue-400 border border-blue-500/20'
    emit('task-started')
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border border-red-500/20'
  }
  setTimeout(() => { message.value = '' }, 8000)
}
</script>
