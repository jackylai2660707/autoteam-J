<template>
  <div>
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-xl font-bold text-white">Team 成员</h2>
        <p class="mt-1 text-sm text-gray-400">按 Team 查看成员与 pending invite；页面只读展示成员，不提供移出或取消邀请操作。</p>
      </div>
      <div class="flex items-center gap-2">
        <select
          v-if="teams.length > 1"
          v-model="selectedAccountId"
          class="rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-white focus:border-blue-500 focus:outline-none"
        >
          <option v-for="team in enabledTeams" :key="team.account_id || team.id" :value="team.account_id">
            {{ team.label || team.workspace_name || team.account_id }}
          </option>
        </select>
        <button @click="fetchMembers" :disabled="loading"
          class="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-sm rounded-lg border border-gray-700 transition disabled:opacity-50 text-gray-300 hover:text-white">
          {{ loading ? '加载中...' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="mb-4 px-4 py-3 rounded-lg text-sm bg-red-500/10 text-red-400 border border-red-500/20">
      {{ error }}
    </div>
    <div v-if="message" class="mb-4 px-4 py-3 rounded-lg text-sm border" :class="messageClass">
      {{ message }}
    </div>

    <div v-if="data" class="space-y-4">
      <!-- 统计 -->
      <div class="flex flex-wrap gap-3 text-sm">
        <span class="px-3 py-1.5 bg-gray-800 rounded-lg text-gray-300">
          Team:
          <span class="text-white font-medium">{{ currentTeamLabel }}</span>
        </span>
        <span class="px-3 py-1.5 bg-gray-800 rounded-lg text-gray-300">成员: <span class="text-white font-medium">{{ data.total }}</span></span>
        <span v-if="data.invites > 0" class="px-3 py-1.5 bg-gray-800 rounded-lg text-gray-300">待接受邀请: <span class="text-yellow-400 font-medium">{{ data.invites }}</span></span>
        <span class="px-3 py-1.5 bg-gray-800 rounded-lg text-gray-300">OAuth active: <span class="text-emerald-400 font-medium">{{ memberOAuthSummary.active }}</span></span>
        <span class="px-3 py-1.5 bg-gray-800 rounded-lg text-gray-300">quota 可用: <span class="text-emerald-400 font-medium">{{ memberQuotaSummary.available }}</span></span>
        <span class="px-3 py-1.5 bg-gray-800 rounded-lg text-gray-300">quota 耗尽缓存: <span class="text-amber-400 font-medium">{{ memberQuotaSummary.exhausted }}</span></span>
      </div>
      <div class="px-4 py-3 rounded-lg text-sm bg-blue-500/10 text-blue-300 border border-blue-500/20">
        当前已按 <code class="px-1 rounded bg-gray-900/70">swap_seat</code> 模式展示：不会移出成员，也不会取消邀请。
        成员行会合并 CPA OAuth 与 quota cache；pending invite 可手动消费对应 CF 邮箱完成注册，注册前旧成员切 Codex，注册后新号切 GPT。
      </div>
      <div
        v-if="selectedTeam"
        class="flex flex-wrap gap-2 rounded-xl border border-white/10 bg-white/[0.03] px-4 py-3 text-xs text-gray-300"
      >
        <span class="font-medium text-white">当前目标：{{ currentTeamLabel }}</span>
        <span class="rounded bg-gray-900/70 px-2 py-0.5">active {{ selectedTeam.max_chatgpt_active || 2 }}</span>
        <span class="rounded bg-gray-900/70 px-2 py-0.5">{{ selectedTeam.session_present ? '独立 session' : '共享默认 session' }}</span>
        <span v-if="selectedTeam.pending_invite_email" class="rounded bg-amber-500/10 px-2 py-0.5 text-amber-200">
          指定 pending {{ selectedTeam.pending_invite_email }}
        </span>
      </div>
      <div
        v-if="currentTeamDisabled"
        class="px-4 py-3 rounded-lg text-sm bg-amber-500/10 text-amber-300 border border-amber-500/20"
      >
        当前 Team 已 disabled，只允许查看缓存/成员；不会提交 pending invite 替换任务。请到「配置面板 → 多 Team」启用后再操作。
      </div>

      <!-- 成员表格 -->
      <div class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-gray-400 text-left border-b border-gray-800">
                <th class="px-4 py-3 font-medium">#</th>
                <th class="px-4 py-3 font-medium">邮箱</th>
                <th class="px-4 py-3 font-medium">角色</th>
                <th class="px-4 py-3 font-medium">类型</th>
                <th class="px-4 py-3 font-medium">Seat</th>
                <th class="px-4 py-3 font-medium">CPA OAuth</th>
                <th class="px-4 py-3 font-medium">Quota cache</th>
                <th class="px-4 py-3 font-medium">来源</th>
                <th class="px-4 py-3 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(m, i) in data.members" :key="memberKey(m)"
                class="border-b border-gray-800/50 hover:bg-gray-800/30 transition">
                <td class="px-4 py-3 text-gray-500">{{ i + 1 }}</td>
                <td class="px-4 py-3 font-mono text-xs text-slate-200">{{ m.email }}</td>
                <td class="px-4 py-3">
                  <span class="px-2 py-0.5 rounded text-xs font-medium"
                    :class="{
                      'bg-purple-500/10 text-purple-400': m.role === 'account-owner',
                      'bg-blue-500/10 text-blue-400': m.role === 'account-admin',
                      'bg-gray-500/10 text-gray-300': m.role !== 'account-owner' && m.role !== 'account-admin',
                    }">
                    {{ m.role || 'member' }}
                  </span>
                </td>
                <td class="px-4 py-3">
                  <span class="px-2 py-0.5 rounded text-xs font-medium"
                    :class="m.type === 'invite' ? 'bg-yellow-500/10 text-yellow-400' : 'bg-green-500/10 text-green-400'">
                    {{ m.type === 'invite' ? '待接受' : '已加入' }}
                  </span>
                </td>
                <td class="px-4 py-3">
                  <span class="px-2 py-0.5 rounded text-xs font-medium" :class="seatClass(m.seat_type)">
                    {{ m.seat_type_label || seatLabel(m.seat_type) }}
                  </span>
                </td>
                <td class="px-4 py-3">
                  <span v-if="m.type === 'invite'" class="text-xs text-gray-500">注册后由 CPA 管理</span>
                  <span v-else class="px-2 py-0.5 rounded text-xs font-medium" :class="oauthClass(m)">
                    {{ oauthLabel(m) }}
                  </span>
                </td>
                <td class="px-4 py-3 text-xs">
                  <div v-if="m.type === 'invite'" class="text-gray-500">等待注册</div>
                  <div v-else-if="quotaEntryFor(m)" class="space-y-1">
                    <span class="inline-flex rounded-full px-2 py-0.5" :class="quotaClass(m)">
                      {{ quotaLabel(m) }}
                    </span>
                    <div class="font-mono text-[11px] text-gray-500">
                      5h {{ quotaEntryFor(m).primary_remaining }}% · weekly {{ quotaEntryFor(m).weekly_remaining }}%
                    </div>
                    <div class="font-mono text-[11px] text-gray-500">
                      下次 {{ formatTs(quotaEntryFor(m).next_check_at || quotaEntryFor(m).exhausted_until) }}
                    </div>
                  </div>
                  <div v-else class="text-gray-500">
                    无记录；下次 swap_seat 会先检查
                  </div>
                </td>
                <td class="px-4 py-3">
                  <span class="text-xs" :class="m.is_local ? 'text-blue-400' : 'text-gray-500'">
                    {{ m.is_local ? '本地管理' : '外部' }}
                  </span>
                </td>
                <td class="px-4 py-3 text-right text-xs text-gray-500">
                  <span v-if="m.type === 'member'">由 swap_seat 自动收敛</span>
                  <button
                    v-else
                    @click="consumePendingInvite(m.email)"
                    :disabled="actionLoading === m.email || !!runningTask || !adminReady || currentTeamDisabled"
                    class="rounded-lg border border-amber-500/40 bg-amber-600/20 px-3 py-1 text-xs font-medium text-amber-200 transition hover:bg-amber-600/30 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {{ actionLoading === m.email ? '提交中...' : '消费此 invite 替换' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Loading -->
    <div v-else-if="loading" class="bg-gray-900 border border-gray-800 rounded-xl h-64 animate-pulse"></div>

    <!-- Empty -->
    <div v-else class="text-center text-gray-500 py-12">
      点击「刷新」加载 Team 成员列表
    </div>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  runningTask: {
    type: Object,
    default: null,
  },
  adminStatus: {
    type: Object,
    default: null,
  },
})
const emit = defineEmits(['task-started'])

const data = ref(null)
const teams = ref([])
const selectedAccountId = ref('')
const loading = ref(false)
const error = ref('')
const message = ref('')
const messageClass = ref('')
const actionLoading = ref('')
const runtimeStatus = ref(null)
const cpaAuths = ref([])
const teamSelectorReady = ref(false)
const adminReady = computed(() => !!props.adminStatus?.configured)
const enabledTeams = computed(() => teams.value.filter(team => team.enabled !== false))
const selectedTeam = computed(() => teams.value.find(team => team.account_id === selectedAccountId.value) || enabledTeams.value[0] || teams.value[0] || null)
const currentTeamDisabled = computed(() => !!selectedTeam.value && selectedTeam.value.enabled === false)
const currentTeamLabel = computed(() => selectedTeam.value?.label || data.value?.team?.label || data.value?.team?.workspace_name || data.value?.team?.account_id || '当前 Team')
const quotaEntries = computed(() => Array.isArray(runtimeStatus.value?.quota_cache?.entries) ? runtimeStatus.value.quota_cache.entries : [])
const memberQuotaSummary = computed(() => {
  const members = (data.value?.members || []).filter(item => item.type === 'member')
  return {
    available: members.filter(item => quotaEntryFor(item)?.quota_available).length,
    exhausted: members.filter(item => quotaEntryFor(item)?.cache_state === 'blocked_until_reset').length,
  }
})
const memberOAuthSummary = computed(() => {
  const members = (data.value?.members || []).filter(item => item.type === 'member')
  return {
    active: members.filter(item => oauthActiveFor(item)).length,
  }
})

const CACHE_PREFIX = 'autoteam_team_members'

function cacheKey() {
  return `${CACHE_PREFIX}:${selectedAccountId.value || 'default'}`
}

function loadCache() {
  try {
    const raw = localStorage.getItem(cacheKey())
    if (raw) {
      const cached = JSON.parse(raw)
      // 缓存 10 分钟有效
      if (cached.time && Date.now() - cached.time < 600000) {
        return cached.data
      }
    }
  } catch {}
  return null
}

function saveCache(d) {
  try {
    localStorage.setItem(cacheKey(), JSON.stringify({ data: d, time: Date.now() }))
  } catch {}
}

function memberKey(member) {
  return `${member.type}:${member.user_id}:${member.email}`
}

function seatLabel(seatType) {
  if (seatType === 'chatgpt') return 'ChatGPT'
  if (seatType === 'codex') return 'Codex'
  return seatType || '-'
}

function seatClass(seatType) {
  if (seatType === 'chatgpt') return 'bg-emerald-600/10 text-emerald-400'
  if (seatType === 'codex') return 'bg-fuchsia-600/10 text-fuchsia-400'
  return 'bg-gray-500/10 text-gray-300'
}

function normalizedEmail(value) {
  return String(value || '').trim().toLowerCase()
}

function currentAccountScope() {
  return selectedAccountId.value || data.value?.team?.account_id || selectedTeam.value?.account_id || ''
}

function cpaAuthsFor(member) {
  const email = normalizedEmail(member?.email)
  if (!email) return []
  return cpaAuths.value.filter(auth => normalizedEmail(auth.email || auth.account) === email)
}

function oauthActiveFor(member) {
  return cpaAuthsFor(member).some(auth => !auth.disabled && String(auth.status || '').toLowerCase() === 'active')
}

function oauthLabel(member) {
  const auths = cpaAuthsFor(member)
  if (!auths.length) return 'CPA missing'
  return oauthActiveFor(member) ? 'active' : 'standby'
}

function oauthClass(member) {
  const auths = cpaAuthsFor(member)
  if (!auths.length) return 'bg-red-500/10 text-red-300'
  return oauthActiveFor(member) ? 'bg-emerald-500/10 text-emerald-300' : 'bg-gray-500/10 text-gray-300'
}

function quotaEntryFor(member) {
  const email = normalizedEmail(member?.email)
  const scope = currentAccountScope()
  if (!email) return null
  const scoped = quotaEntries.value.find(entry => normalizedEmail(entry.email) === email && (!scope || entry.account_id === scope))
  if (scoped) return scoped
  return quotaEntries.value.find(entry => normalizedEmail(entry.email) === email) || null
}

function quotaLabel(member) {
  const entry = quotaEntryFor(member)
  if (!entry) return '无记录'
  if (entry.cache_state === 'blocked_until_reset') return `耗尽:${entry.window || 'reset'}`
  if (entry.cache_state === 'recent_ok') return '近期可用'
  if (entry.cache_state === 'stale') return '待复查'
  return entry.status || '-'
}

function quotaClass(member) {
  const entry = quotaEntryFor(member)
  if (!entry) return 'bg-gray-500/10 text-gray-300'
  if (entry.cache_state === 'blocked_until_reset') return 'bg-amber-500/10 text-amber-300'
  if (entry.quota_available) return 'bg-emerald-500/10 text-emerald-300'
  return 'bg-gray-500/10 text-gray-300'
}

function formatTs(ts) {
  const value = Number(ts || 0)
  if (!value) return '-'
  const d = new Date(value * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function fetchMembers() {
  loading.value = true
  error.value = ''
  try {
    const [membersResult, runtimeResult, cpaResult] = await Promise.all([
      api.getTeamMembers(selectedAccountId.value),
      api.getSwapRuntimeStatus().catch(() => null),
      api.getCpaFiles().catch(() => []),
    ])
    data.value = membersResult
    runtimeStatus.value = runtimeResult
    cpaAuths.value = Array.isArray(cpaResult) ? cpaResult : Array.isArray(cpaResult?.files) ? cpaResult.files : []
    saveCache(data.value)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadTeams() {
  try {
    const result = await api.getTeams()
    teams.value = Array.isArray(result.teams) ? result.teams : []
    if (!selectedAccountId.value && enabledTeams.value.length) {
      selectedAccountId.value = enabledTeams.value[0].account_id || ''
    } else if (selectedAccountId.value && !enabledTeams.value.some(team => team.account_id === selectedAccountId.value)) {
      selectedAccountId.value = enabledTeams.value[0]?.account_id || ''
    }
  } catch (e) {
    // 旧后端或配置未就绪时仍保留单 Team 查询能力。
    teams.value = []
  }
}

async function consumePendingInvite(email) {
  if (!email) return
  if (currentTeamDisabled.value) {
    message.value = '当前 Team 已 disabled，不会提交 pending invite 替换任务'
    messageClass.value = 'bg-amber-500/10 text-amber-300 border-amber-500/20'
    return
  }
  if (!selectedAccountId.value && enabledTeams.value.length > 1) {
    message.value = '请先选择目标 Team，再消费 pending invite'
    messageClass.value = 'bg-amber-500/10 text-amber-300 border-amber-500/20'
    return
  }
  actionLoading.value = email
  error.value = ''
  message.value = ''
  try {
    const result = await api.startConsumePendingInvite(email, selectedAccountId.value)
    message.value = `已提交 pending invite 替换任务: ${result.task_id}`
    messageClass.value = 'bg-blue-500/10 text-blue-300 border-blue-500/20'
    emit('task-started')
  } catch (e) {
    message.value = e.message
    messageClass.value = 'bg-red-500/10 text-red-400 border-red-500/20'
  } finally {
    actionLoading.value = ''
  }
}

watch(selectedAccountId, () => {
  if (!teamSelectorReady.value) return
  data.value = null
  const cached = loadCache()
  if (cached) {
    data.value = cached
  } else {
    fetchMembers()
  }
})

onMounted(async () => {
  await loadTeams()
  teamSelectorReady.value = true
  const cached = loadCache()
  if (cached) {
    data.value = cached
  } else {
    await fetchMembers()
  }
})
</script>
