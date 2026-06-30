<template>
  <div>
    <div class="mb-6">
      <div class="mb-2 inline-flex items-center gap-2 rounded-full border border-blue-400/20 bg-blue-500/10 px-3 py-1 text-xs text-blue-200">
        <span class="inline-block h-2 w-2 rounded-full bg-cyan-300 shadow-[0_0_12px_rgba(103,232,249,0.9)]"></span>
        team operations control plane
      </div>
      <h2 class="text-2xl font-bold text-white">Team 操作 / Seat 调度</h2>
      <p class="mt-2 max-w-4xl text-sm leading-6 text-gray-400">
        批量发送 invite、清空 pending invite、swap_seat 和多 Team 自动调度都在这里。只管理 AutoTeam 生成/登记的 auth 和受管 Team member；外部 CPA auth 只保护不使用、不查 quota、不启停、不删除。
      </p>
    </div>

    <div class="mb-6 grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
      <div class="rounded-2xl border border-white/10 bg-gray-900/70 p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-sm font-semibold text-white">受管 Team</h3>
            <p class="mt-1 text-xs text-gray-500">来自 <code>TEAM_WORKSPACES_JSON</code>；留空时回退当前管理员 Team。</p>
          </div>
          <button @click="loadTeams" class="rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700">
            刷新
          </button>
        </div>
        <div v-if="teamLoading" class="h-24 animate-pulse rounded-xl bg-gray-800/70"></div>
        <div v-else-if="teams.length" class="grid gap-3 md:grid-cols-2">
          <button
            v-for="team in teams"
            :key="team.account_id || team.id"
            type="button"
            @click="selectTeam(team)"
            :disabled="team.enabled === false"
            class="rounded-xl border p-3 text-left transition hover:border-blue-400/40 hover:bg-blue-500/5 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-white/10 disabled:hover:bg-white/[0.03]"
            :class="selectedTeamAccountId === team.account_id
              ? 'border-blue-400/50 bg-blue-500/10 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]'
              : 'border-white/10 bg-white/[0.03]'"
          >
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="truncate text-sm font-medium text-white">{{ team.label || team.workspace_name || team.account_id }}</div>
                <div class="mt-1 truncate font-mono text-[11px] text-gray-500">{{ team.account_id }}</div>
              </div>
              <span
                class="rounded-full px-2 py-0.5 text-[11px]"
                :class="selectedTeamAccountId === team.account_id
                  ? 'bg-blue-400/15 text-blue-100'
                  : team.enabled === false ? 'bg-gray-500/10 text-gray-400' : 'bg-emerald-500/10 text-emerald-300'"
              >
                {{ selectedTeamAccountId === team.account_id ? '操作目标' : team.enabled === false ? 'disabled' : 'enabled' }}
              </span>
            </div>
            <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-gray-400">
              <span class="rounded bg-gray-800 px-2 py-1">目标 active {{ team.max_chatgpt_active || 2 }}</span>
              <span class="rounded bg-gray-800 px-2 py-1">{{ team.session_present ? 'session ok' : '共享默认 session' }}</span>
              <span v-if="team.pending_invite_email" class="rounded bg-amber-500/10 px-2 py-1 text-amber-300">指定 invite</span>
              <span v-if="team.invite_domains" class="rounded bg-emerald-500/10 px-2 py-1 text-emerald-300">域名池 {{ inviteDomainCount(team.invite_domains) }}</span>
            </div>
            <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-gray-400">
              <span class="rounded px-2 py-1" :class="cooldownFor(team)?.allowed ? 'bg-emerald-500/10 text-emerald-300' : cooldownFor(team) ? 'bg-amber-500/10 text-amber-300' : 'bg-gray-800 text-gray-400'">
                {{ cooldownLabel(team) }}
              </span>
              <span class="rounded bg-emerald-500/10 px-2 py-1 text-emerald-300">
                自管 quota 可用 {{ quotaSummaryForTeam(team).available }}
              </span>
              <span class="rounded bg-amber-500/10 px-2 py-1 text-amber-300">
                耗尽 {{ quotaSummaryForTeam(team).exhausted }}
              </span>
              <span v-if="quotaSummaryForTeam(team).stale" class="rounded bg-gray-800 px-2 py-1">
                待复查 {{ quotaSummaryForTeam(team).stale }}
              </span>
            </div>
          </button>
        </div>
        <div v-else class="rounded-xl border border-dashed border-gray-700 px-4 py-6 text-sm text-gray-500">
          未读取到 Team 配置。请先完成管理员登录，或在「配置面板 → 多 Team」填写 TEAM_WORKSPACES_JSON。
        </div>
      </div>

      <div class="rounded-2xl border border-blue-500/20 bg-blue-500/10 p-4 text-sm leading-6 text-blue-200">
        <div class="font-medium text-blue-100">调度策略与保护边界</div>
        <ol class="mt-2 list-decimal space-y-1 pl-5 text-xs text-blue-200/90">
          <li>只检查 AutoTeam 受管成员对应的受管 CPA auth quota。</li>
          <li>只保留 1~5 个自管 quota 可用账号为 ChatGPT/OAuth active，其余受管账号 Codex + disabled standby。</li>
          <li>外部/手动 CPA auth 和非受管 Team member 不使用、不查 quota、不启停、不删除。</li>
          <li>无可用 quota 时不做无用 swap；开启自动补位后才消费 pending invite 或创建新 invite。</li>
        </ol>
      </div>
    </div>

    <div class="mb-6 grid gap-4 lg:grid-cols-[0.95fr_1.05fr]">
      <div class="rounded-2xl border border-white/10 bg-gray-900/70 p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-sm font-semibold text-white">swap 冷却</h3>
            <p class="mt-1 text-xs text-gray-500">按 Team / account_id 隔离：每天 3 次，每次间隔 2 小时。</p>
          </div>
          <button @click="loadRuntimeStatus" :disabled="runtimeLoading" class="rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50">
            {{ runtimeLoading ? '刷新中...' : '刷新状态' }}
          </button>
        </div>
        <div v-if="runtimeLoading" class="h-20 animate-pulse rounded-xl bg-gray-800/70"></div>
        <div v-else-if="runtimeTeams.length" class="space-y-2">
          <div v-for="item in runtimeTeams" :key="item.team?.account_id || item.cooldown?.scope" class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
            <div class="flex items-center justify-between gap-3">
              <div class="min-w-0">
                <div class="truncate text-sm text-white">{{ item.team?.label || item.team?.workspace_name || item.team?.account_id || item.cooldown?.scope }}</div>
                <div class="mt-1 font-mono text-[11px] text-gray-500">{{ item.cooldown?.scope }}</div>
              </div>
              <span class="rounded-full px-2 py-0.5 text-[11px]" :class="item.cooldown?.allowed ? 'bg-emerald-500/10 text-emerald-300' : 'bg-amber-500/10 text-amber-300'">
                {{ item.cooldown?.allowed ? '可 swap' : '冷却中' }}
              </span>
            </div>
            <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-gray-400">
              <span class="rounded bg-gray-800 px-2 py-1">today {{ item.cooldown?.used_today || 0 }}/{{ item.cooldown?.daily_limit || 3 }}</span>
              <span class="rounded bg-gray-800 px-2 py-1">剩余 {{ item.cooldown?.remaining_today ?? '-' }}</span>
              <span class="rounded bg-gray-800 px-2 py-1">下次 {{ formatTs(item.cooldown?.next_allowed_at) }}</span>
            </div>
          </div>
        </div>
        <div v-else class="rounded-xl border border-dashed border-gray-700 px-4 py-6 text-sm text-gray-500">
          暂无冷却状态；完成管理员登录或配置 Team 后会显示。
        </div>
      </div>

      <div class="rounded-2xl border border-white/10 bg-gray-900/70 p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-sm font-semibold text-white">受管 quota 一眼看</h3>
            <p class="mt-1 text-xs text-gray-500">只读展示上次巡检记录：适用 quota 窗口、记录新鲜度和 reset 时间；单窗口长期额度不会显示 5h，只包含 AutoTeam 受管 OAuth。</p>
          </div>
          <div class="flex flex-wrap gap-2 text-[11px] text-gray-400">
            <span class="rounded bg-emerald-500/10 px-2 py-1 text-emerald-300">available {{ quotaSummary.available || 0 }}</span>
            <span class="rounded bg-amber-500/10 px-2 py-1 text-amber-300">exhausted {{ quotaSummary.exhausted_cached || 0 }}</span>
            <span class="rounded bg-gray-800 px-2 py-1">stale {{ quotaSummary.stale || 0 }}</span>
          </div>
        </div>
        <div v-if="runtimeLoading" class="h-20 animate-pulse rounded-xl bg-gray-800/70"></div>
        <div v-else-if="quotaEntries.length" class="space-y-3">
          <div class="rounded-xl border px-4 py-3" :class="quotaOverviewClass">
            <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div class="text-sm font-semibold text-white">{{ quotaOverview.title }}</div>
                <div class="mt-1 text-xs leading-5 text-gray-300">{{ quotaOverview.detail }}</div>
              </div>
              <div class="shrink-0 rounded-full px-3 py-1 text-xs font-medium" :class="quotaOverview.badgeClass">
                最低剩余 {{ quotaOverview.minRemaining }}%
              </div>
            </div>
          </div>

          <div class="grid gap-3 md:grid-cols-2">
            <div v-for="entry in quotaEntries" :key="entry.key" class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="truncate font-mono text-xs text-slate-200">{{ entry.email || '-' }}</div>
                  <div class="mt-1 truncate font-mono text-[11px] text-gray-500">{{ teamLabel(entry.account_id) }}</div>
                </div>
                <span class="shrink-0 rounded-full px-2 py-0.5 text-[11px]" :class="quotaStateClass(entry)">
                  {{ quotaStateLabel(entry) }}
                </span>
              </div>

              <div class="mt-3 space-y-2">
                <QuotaBar label="5h" :value="entry.primary_remaining" :applicable="entry.primary_applicable !== false" />
                <QuotaBar label="weekly" :value="entry.weekly_remaining" :applicable="entry.weekly_applicable !== false" />
                <QuotaBar label="monthly" :value="entry.monthly_remaining" :applicable="entry.monthly_applicable !== false" />
              </div>

              <div class="mt-3 grid grid-cols-2 gap-2 text-[11px] text-gray-500">
                <div class="rounded bg-gray-950/40 px-2 py-1">
                  上次记录 <span class="text-gray-300">{{ formatTs(entry.updated_at) }}</span>
                </div>
                <div class="rounded bg-gray-950/40 px-2 py-1">
                  {{ entry.cache_state === 'blocked_until_reset' ? 'reset 后可复用' : '下次复查' }}
                  <span class="text-gray-300">{{ quotaNextCheckLabel(entry) }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="rounded-xl border border-dashed border-gray-700 px-4 py-6 text-sm text-gray-500">
          暂无受管 quota 记录。第一次执行 swap_seat 或等待巡检后，会记录每个受管 OAuth 的适用窗口、剩余额度和 reset 时间。
        </div>
      </div>
    </div>
    <TaskPanel
      mode="pool"
      :teams="teams"
      v-model:target-account-id="selectedTeamAccountId"
      :running-task="runningTask"
      :admin-status="adminStatus"
      @task-started="handleTaskStarted"
      @refresh="$emit('refresh')"
    />

    <div class="mt-6 rounded-2xl border border-white/10 bg-gray-900/70 p-4">
      <div class="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 class="text-sm font-semibold text-white">AutoTeam 受管 CPA OAuth</h3>
          <p class="mt-1 text-xs leading-5 text-gray-500">
            这里只显示本系统创建/登记的 auth；外部 CPA auth 受保护，不显示为可操作对象。只允许手动 disable，enable 必须由 swap_seat 根据 quota 和保留数自动选择。
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <span class="rounded bg-emerald-500/10 px-2 py-1 text-xs text-emerald-300">受管 active {{ cpaSummary.active }}</span>
          <span class="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300">standby {{ cpaSummary.standby }}</span>
          <span v-if="cpaProtectedUnmanaged" class="rounded bg-amber-500/10 px-2 py-1 text-xs text-amber-300">已保护外部 {{ cpaProtectedUnmanaged }}</span>
          <button @click="loadCpaAuths" :disabled="cpaLoading" class="rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50">
            {{ cpaLoading ? '刷新中...' : '刷新 CPA' }}
          </button>
        </div>
      </div>

      <div v-if="cpaError" class="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
        {{ cpaError }}
      </div>
      <div v-else-if="cpaLoading" class="h-24 animate-pulse rounded-xl bg-gray-800/70"></div>
      <div v-else-if="codexAuths.length" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <div v-for="auth in codexAuths" :key="auth.name || auth.id || auth.auth_index" class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="truncate font-mono text-xs text-slate-200">{{ authEmail(auth) }}</div>
              <div class="mt-1 truncate font-mono text-[11px] text-gray-500">{{ auth.name || auth.id || auth.auth_index }}</div>
            </div>
            <span class="rounded-full px-2 py-0.5 text-[11px]" :class="authActive(auth) ? 'bg-emerald-500/10 text-emerald-300' : 'bg-gray-500/10 text-gray-400'">
              {{ authActive(auth) ? 'active' : 'standby' }}
            </span>
          </div>
          <div class="mt-3 flex items-center justify-between gap-3">
            <div class="min-w-0 text-[11px] text-gray-500">
              <div>
                status={{ auth.status || '-' }} · disabled={{ auth.disabled ? 'true' : 'false' }}
              </div>
              <div v-if="quotaEntryForAuth(auth)" class="mt-1 flex flex-wrap gap-1.5">
                <span class="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
                  5h {{ quotaWindowText(quotaEntryForAuth(auth), 'primary') }}
                </span>
                <span class="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
                  weekly {{ quotaWindowText(quotaEntryForAuth(auth), 'weekly') }}
                </span>
                <span class="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
                  monthly {{ quotaWindowText(quotaEntryForAuth(auth), 'monthly') }}
                </span>
                <span class="rounded px-2 py-0.5" :class="quotaStateClass(quotaEntryForAuth(auth))">
                  {{ quotaStateLabel(quotaEntryForAuth(auth)) }}
                </span>
                <span class="rounded bg-gray-800 px-2 py-0.5 text-gray-400">
                  下次 {{ quotaNextCheckLabel(quotaEntryForAuth(auth)) }}
                </span>
              </div>
              <div v-else class="mt-1 text-amber-300/80">
                暂无受管 quota 记录，下一次 swap_seat/巡检会记录
              </div>
            </div>
            <div class="flex shrink-0 flex-col gap-2 sm:flex-row">
              <button
                v-if="authActive(auth)"
                @click="disableCpaAuth(auth)"
                :disabled="!!runningTask || cpaActionName === authName(auth)"
                class="rounded-lg border border-amber-500/40 bg-amber-600/20 px-3 py-1 text-xs font-medium text-amber-200 transition hover:bg-amber-600/30 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {{ cpaActionName === authName(auth) ? '处理中...' : 'disable' }}
              </button>
              <button
                @click="forgetCpaAuth(auth)"
                :disabled="!!runningTask || cpaActionName === authName(auth)"
                class="rounded-lg border border-rose-500/40 bg-rose-600/20 px-3 py-1 text-xs font-medium text-rose-200 transition hover:bg-rose-600/30 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {{ cpaActionName === authName(auth) ? '处理中...' : '清理' }}
              </button>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="rounded-xl border border-dashed border-gray-700 px-4 py-6 text-sm text-gray-500">
        暂未读取到 AutoTeam 受管 CPA Codex OAuth。请先通过系统创建 invite/PAT，或确认 managed_cpa_auths.json 中有登记。
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, defineComponent, h, onMounted, ref } from 'vue'
import TaskPanel from './TaskPanel.vue'
import { api } from '../api.js'

const props = defineProps({
  runningTask: Object,
  adminStatus: Object,
})

const emit = defineEmits(['task-started', 'refresh'])

const teams = ref([])
const selectedTeamAccountId = ref('')
const teamLoading = ref(false)
const runtimeStatus = ref(null)
const runtimeLoading = ref(false)
const cpaAuths = ref([])
const cpaFilesSummary = ref({})
const cpaLoading = ref(false)
const cpaError = ref('')
const cpaActionName = ref('')

const codexAuths = computed(() => cpaAuths.value.filter(auth => {
  const provider = String(auth.provider || auth.type || '').toLowerCase()
  return (!provider || provider === 'codex') && authEmail(auth)
}))

const cpaSummary = computed(() => ({
  active: codexAuths.value.filter(authActive).length,
  standby: codexAuths.value.filter(auth => !authActive(auth)).length,
}))
const cpaProtectedUnmanaged = computed(() => Number(cpaFilesSummary.value?.protected_unmanaged || 0))

const runtimeTeams = computed(() => Array.isArray(runtimeStatus.value?.teams) ? runtimeStatus.value.teams : [])
const quotaSummary = computed(() => runtimeStatus.value?.quota_cache?.summary || {})
const allQuotaEntries = computed(() => runtimeStatus.value?.quota_cache?.entries || [])
const managedAuthNames = computed(() => new Set(codexAuths.value.map(authName).filter(Boolean)))
const managedAuthEmails = computed(() => new Set(codexAuths.value.map(authEmail).filter(Boolean)))
const visibleQuotaEntries = computed(() => allQuotaEntries.value.filter(entry =>
  managedAuthNames.value.has(String(entry.auth_id || '').trim())
  || managedAuthEmails.value.has(normalizedEmail(entry.email))
))
const quotaEntries = computed(() => visibleQuotaEntries.value.slice(0, 8))
const quotaOverview = computed(() => {
  const entries = visibleQuotaEntries.value
  if (!entries.length) {
    return {
      title: '暂无 quota 记录',
      detail: '执行 swap_seat 或等待下一轮巡检后会记录受管 OAuth 的剩余额度。',
      minRemaining: 0,
      badgeClass: 'bg-gray-500/10 text-gray-300',
      level: 'empty',
    }
  }
  const exhausted = entries.filter(entry => entry.cache_state === 'blocked_until_reset' || !entry.quota_available)
  const stale = entries.filter(entry => entry.cache_state === 'stale')
  const minRemaining = Math.min(...entries.map(minQuotaRemaining))
  if (exhausted.length) {
    return {
      title: '有受管账号 quota 已耗尽',
      detail: `${exhausted.length} 个账号会等 reset 后再参与调度，当前不会被选为 active。`,
      minRemaining,
      badgeClass: 'bg-amber-500/10 text-amber-300',
      level: 'warn',
    }
  }
  if (minRemaining <= 20) {
    return {
      title: 'quota 偏低，建议观察',
      detail: `最低窗口只剩 ${minRemaining}%，下一轮巡检会重新确认并按 quota 自动选 active。`,
      minRemaining,
      badgeClass: 'bg-amber-500/10 text-amber-300',
      level: 'warn',
    }
  }
  if (stale.length) {
    return {
      title: '上次记录可用，但需要复查',
      detail: `${stale.length} 条记录已超过快速缓存时间；下一轮巡检会重新打 CPA quota 检查。`,
      minRemaining,
      badgeClass: 'bg-sky-500/10 text-sky-300',
      level: 'stale',
    }
  }
  return {
    title: '受管 quota 状态健康',
    detail: `当前 ${entries.length} 个受管 OAuth 都有可用 quota，swap 会按剩余量自动选择 active。`,
    minRemaining,
    badgeClass: 'bg-emerald-500/10 text-emerald-300',
    level: 'ok',
  }
})
const quotaOverviewClass = computed(() => {
  if (quotaOverview.value.level === 'warn') return 'border-amber-500/20 bg-amber-500/10'
  if (quotaOverview.value.level === 'stale') return 'border-sky-500/20 bg-sky-500/10'
  if (quotaOverview.value.level === 'ok') return 'border-emerald-500/20 bg-emerald-500/10'
  return 'border-white/10 bg-white/[0.03]'
})
const cooldownByAccount = computed(() => {
  const map = new Map()
  for (const item of runtimeTeams.value) {
    const accountId = String(item?.team?.account_id || item?.cooldown?.scope || '').trim()
    if (accountId) map.set(accountId, item.cooldown || null)
  }
  return map
})

function authEmail(auth) {
  return String(auth?.email || auth?.account || '').trim().toLowerCase()
}

function authActive(auth) {
  return !auth?.disabled && String(auth?.status || '').toLowerCase() === 'active'
}

function authName(auth) {
  return String(auth?.name || auth?.id || auth?.auth_index || '').trim()
}

function normalizedEmail(value) {
  return String(value || '').trim().toLowerCase()
}

function quotaEntryForAuth(auth) {
  const email = authEmail(auth)
  const name = authName(auth)
  const accountId = String(auth?.account_id || auth?.accountId || auth?.chatgpt_account_id || '').trim()

  if (name) {
    const byName = visibleQuotaEntries.value.find(entry => String(entry.auth_id || '').trim() === name)
    if (byName) return byName
  }

  if (!email) return null
  const scoped = visibleQuotaEntries.value.find(entry =>
    normalizedEmail(entry.email) === email
    && (!accountId || String(entry.account_id || '').trim() === accountId)
  )
  if (scoped) return scoped
  return visibleQuotaEntries.value.find(entry => normalizedEmail(entry.email) === email) || null
}

async function loadTeams() {
  teamLoading.value = true
  try {
    const result = await api.getTeams()
    teams.value = Array.isArray(result.teams) ? result.teams : []
    const enabledTeams = teams.value.filter(team => team.enabled !== false)
    if (enabledTeams.length === 1 && enabledTeams[0]?.account_id) {
      selectedTeamAccountId.value = enabledTeams[0].account_id
    } else if (
      selectedTeamAccountId.value
      && !enabledTeams.some(team => team.account_id === selectedTeamAccountId.value)
    ) {
      selectedTeamAccountId.value = ''
    }
  } catch {
    teams.value = []
    selectedTeamAccountId.value = ''
  } finally {
    teamLoading.value = false
  }
}

function selectTeam(team) {
  if (team?.enabled === false) return
  selectedTeamAccountId.value = String(team?.account_id || '').trim()
}

function cooldownFor(team) {
  const accountId = String(team?.account_id || '').trim()
  return accountId ? cooldownByAccount.value.get(accountId) : null
}

function cooldownLabel(team) {
  const cooldown = cooldownFor(team)
  if (!cooldown) return 'cooldown 未记录'
  if (cooldown.allowed) return `可 swap · 今日 ${cooldown.used_today || 0}/${cooldown.daily_limit || 3}`
  return `冷却中 · ${formatTs(cooldown.next_allowed_at)}`
}

function quotaSummaryForTeam(team) {
  const accountId = String(team?.account_id || '').trim()
  const entries = visibleQuotaEntries.value.filter(entry => String(entry.account_id || '').trim() === accountId)
  return {
    available: entries.filter(entry => entry.quota_available).length,
    exhausted: entries.filter(entry => entry.cache_state === 'blocked_until_reset').length,
    stale: entries.filter(entry => entry.cache_state === 'stale').length,
  }
}

function teamLabel(accountId) {
  const id = String(accountId || '').trim()
  if (!id) return 'default'
  const team = teams.value.find(item => String(item.account_id || '').trim() === id)
  return team?.label || team?.workspace_name || id
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, Number(value || 0)))
}

function minQuotaRemaining(entry) {
  const values = []
  if (entry?.primary_applicable !== false) values.push(clampPercent(entry?.primary_remaining))
  if (entry?.weekly_applicable !== false) values.push(clampPercent(entry?.weekly_remaining))
  if (entry?.monthly_applicable !== false) values.push(clampPercent(entry?.monthly_remaining))
  return values.length ? Math.min(...values) : 0
}

function quotaWindowText(entry, window) {
  if (entry?.[`${window}_applicable`] === false) return 'N/A'
  return `${clampPercent(entry?.[`${window}_remaining`])}%`
}

function quotaNextCheckLabel(entry) {
  const ts = entry?.next_check_at || entry?.exhausted_until
  if (ts) return formatTs(ts)
  if (entry?.cache_state === 'stale') return '下轮巡检'
  return '-'
}

function inviteDomainCount(value) {
  return String(value || '').split(/[;,\n]+/).filter(part => part.trim()).length
}

async function loadRuntimeStatus() {
  runtimeLoading.value = true
  try {
    runtimeStatus.value = await api.getSwapRuntimeStatus()
  } catch {
    runtimeStatus.value = null
  } finally {
    runtimeLoading.value = false
  }
}

async function loadCpaAuths() {
  cpaLoading.value = true
  cpaError.value = ''
  try {
    const result = await api.getCpaFiles()
    cpaAuths.value = Array.isArray(result) ? result : Array.isArray(result.files) ? result.files : []
    cpaFilesSummary.value = result?.summary || {}
  } catch (e) {
    cpaError.value = e.message
    cpaAuths.value = []
    cpaFilesSummary.value = {}
  } finally {
    cpaLoading.value = false
  }
}

async function disableCpaAuth(auth) {
  const name = authName(auth)
  if (!name || props.runningTask) return
  cpaActionName.value = name
  cpaError.value = ''
  try {
    await api.setCpaAuthDisabled(name, true)
    await loadCpaAuths()
  } catch (e) {
    cpaError.value = e.message
  } finally {
    cpaActionName.value = ''
  }
}

async function forgetCpaAuth(auth) {
  const name = authName(auth)
  if (!name || props.runningTask) return
  const email = authEmail(auth)
  const ok = window.confirm(
    `确认清理这个 AutoTeam 受管 auth 吗？\n\n${email || name}\n\n会删除 CPA auth、移出受管登记、清除 quota 记录；不会移除 Team member，也不会碰外部 auth。`
  )
  if (!ok) return
  cpaActionName.value = name
  cpaError.value = ''
  try {
    await api.forgetCpaAuth(name)
    await Promise.all([loadCpaAuths(), loadRuntimeStatus()])
  } catch (e) {
    cpaError.value = e.message
  } finally {
    cpaActionName.value = ''
  }
}

function formatTs(ts) {
  const value = Number(ts || 0)
  if (!value) return '-'
  const d = new Date(value * 1000)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function quotaStateLabel(entry) {
  if (entry.cache_state === 'blocked_until_reset') return `耗尽:${entry.window || 'reset'}`
  if (entry.cache_state === 'recent_ok') return '近期可用'
  if (entry.cache_state === 'stale') return entry.quota_available ? '可用待复查' : '待复查'
  return entry.status || '-'
}

function quotaStateClass(entry) {
  if (entry.cache_state === 'blocked_until_reset') return 'bg-amber-500/10 text-amber-300'
  if (entry.cache_state === 'recent_ok') return 'bg-emerald-500/10 text-emerald-300'
  if (entry.cache_state === 'stale') return entry.quota_available ? 'bg-sky-500/10 text-sky-300' : 'bg-gray-500/10 text-gray-300'
  return entry.quota_available ? 'bg-emerald-500/10 text-emerald-300' : 'bg-gray-500/10 text-gray-300'
}

const QuotaBar = defineComponent({
  name: 'QuotaBar',
  props: {
    label: { type: String, required: true },
    value: { type: Number, default: 0 },
    applicable: { type: Boolean, default: true },
  },
  setup(props) {
    return () => {
      const value = clampPercent(props.value)
      if (!props.applicable) {
        return h('div', { class: 'grid grid-cols-[4.5rem_1fr_3rem] items-center gap-2 text-[11px]' }, [
          h('span', { class: 'text-gray-400' }, props.label),
          h('div', { class: 'h-2 rounded-full bg-gray-800/60' }),
          h('span', { class: 'text-right font-mono text-gray-500' }, 'N/A'),
        ])
      }
      const color = value <= 0
        ? 'bg-rose-400'
        : value <= 20
          ? 'bg-amber-400'
          : 'bg-emerald-400'
      return h('div', { class: 'grid grid-cols-[4.5rem_1fr_3rem] items-center gap-2 text-[11px]' }, [
        h('span', { class: 'text-gray-400' }, props.label),
        h('div', { class: 'h-2 overflow-hidden rounded-full bg-gray-800' }, [
          h('div', {
            class: `h-full rounded-full ${color}`,
            style: { width: `${value}%` },
          }),
        ]),
        h('span', { class: 'text-right font-mono text-gray-300' }, `${value}%`),
      ])
    }
  },
})

function handleTaskStarted() {
  loadRuntimeStatus()
  loadCpaAuths()
  emit('task-started')
}

onMounted(() => {
  loadTeams()
  loadRuntimeStatus()
  loadCpaAuths()
})
</script>
