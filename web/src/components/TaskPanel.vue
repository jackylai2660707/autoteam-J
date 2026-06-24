<template>
  <div class="mt-6 bg-gray-900 border border-gray-800 rounded-xl p-4">
    <h2 class="text-lg font-semibold text-white mb-4">{{ panelTitle }}</h2>
    <div v-if="showAdminHint" class="mb-4 px-4 py-3 rounded-lg text-sm border bg-amber-500/10 text-amber-300 border-amber-500/20">
      {{ adminHint }}
    </div>
    <div v-if="mode === 'pool'" class="mb-4 px-4 py-3 rounded-lg text-sm border bg-blue-500/10 text-blue-300 border-blue-500/20">
      冷却限制：每天最多执行 3 次 swap_seat；每次 swap_seat 至少间隔 2 小时。冷却由后端强制执行，不能绕过。
      自动替换只会在 quota 全耗尽时消费已有 pending invite；如需指定某个 pending invite，请到「Team 成员」页选择对应 Team 后点击该 invite 的替换按钮。
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
        <input v-model.number="paramValue" type="number" min="1" :max="pendingAction?.paramName === 'max_chatgpt_active' ? 5 : 20"
          class="w-20 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" />
        <button @click="confirmAction" :disabled="pendingAction && isDisabled(pendingAction)"
          class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition">
          确认执行
        </button>
        <button @click="showParams = false"
          class="px-3 py-1.5 text-gray-400 hover:text-white text-sm transition">
          取消
        </button>
      </div>
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
          <span class="font-medium text-amber-50">quota 全耗尽时消费 pending invite</span>
          <span class="block text-amber-200/80">
            开启后，每个 Team 只有在现有成员没有任何可用 5h + weekly quota 时，才会使用该 Team 的已有 pending invite 注册新号；关闭则只做 swap_seat 收敛。
          </span>
        </span>
      </label>
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
  if (props.mode === 'pool') return 'Seat 调度'
  return '操作'
})
const adminHint = computed(() => {
  return '请先在「配置面板」页完成管理员登录；swap_seat 只会切换 seat 和 CPA OAuth active/disabled，不会 kick Team 成员。'
})
const showAdminHint = computed(() => !adminReady.value && props.mode === 'pool')

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
  return multiTeamMode.value && ['swap-seats', 'auto-detect-replace'].includes(action.key)
}

function isDisabled(action) {
  if (props.runningTask) return true
  if (!adminReady.value && !action.allowWithoutAdmin) return true
  if (requiresExplicitTeam(action) && !selectedAccountId.value) return true
  return false
}

async function execute(action) {
  if (isDisabled(action)) return
  message.value = ''
  if (action.needParam) {
    pendingAction.value = action
    replaceWithPendingInvite.value = true
    paramLabel.value = action.key === 'manage-teams'
      ? '默认 ChatGPT/OAuth active 保留数（1~5；Team 配置优先）'
      : action.paramName === 'max_chatgpt_active'
        ? 'ChatGPT/OAuth active 保留数（1~5）'
        : '目标成员数'
    paramValue.value = action.paramName === 'max_chatgpt_active'
      ? Number(action.key === 'swap-seats' ? (selectedTeam.value?.max_chatgpt_active || 2) : 2)
      : 5
    showParams.value = true
    return
  }
  await doExecute(action)
}

async function confirmAction() {
  showParams.value = false
  if (pendingAction.value) {
    await doExecute(pendingAction.value, paramValue.value)
    pendingAction.value = null
  }
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
    } else if (action.key === 'manage-teams') {
      result = await api.startManageTeams(normalizedParam, replaceWithPendingInvite.value)
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
