const BASE = '/api'

function getApiKey() {
  return localStorage.getItem('autoteam_api_key') || ''
}

export function setApiKey(key) {
  localStorage.setItem('autoteam_api_key', key)
}

export function clearApiKey() {
  localStorage.removeItem('autoteam_api_key')
}

async function request(method, path, body = null) {
  const headers = { 'Content-Type': 'application/json' }
  const key = getApiKey()
  if (key) {
    headers['Authorization'] = `Bearer ${key}`
  }
  const opts = { method, headers }
  if (body) opts.body = JSON.stringify(body)
  const resp = await fetch(`${BASE}${path}`, opts)
  let data
  try {
    data = await resp.json()
  } catch {
    const err = new Error(`HTTP ${resp.status}: 服务器返回了非 JSON 响应`)
    err.status = resp.status
    throw err
  }
  if (!resp.ok) {
    const msg = data?.message || data?.detail?.message || data?.detail || `HTTP ${resp.status}`
    const err = new Error(msg)
    err.status = resp.status
    throw err
  }
  return data
}

export const api = {
  checkAuth: () => request('GET', '/auth/check'),
  getSetupStatus: () => request('GET', '/setup/status'),
  saveSetup: (config) => request('POST', '/setup/save', config),
  getRuntimeConfig: () => request('GET', '/config/runtime'),
  saveRuntimeConfig: (config) => request('PUT', '/config/runtime', config),
  getRuntimeConfigSource: () => request('GET', '/config/source'),
  saveRuntimeConfigSource: (payload) => request('PUT', '/config/source', payload),

  getAdminStatus: () => request('GET', '/admin/status'),
  getCpaFiles: () => request('GET', '/cpa/files'),
  setCpaAuthDisabled: (name, disabled) => request('PATCH', '/cpa/auth/status', { name, disabled }),
  forgetCpaAuth: (name) => request('POST', '/cpa/auth/forget', { name }),

  startAdminLogin: (email) => request('POST', '/admin/login/start', { email }),
  submitAdminSession: (email, sessionToken) => request('POST', '/admin/login/session', { email, session_token: sessionToken }),
  submitAdminPassword: (password) => request('POST', '/admin/login/password', { password }),
  submitAdminCode: (code) => request('POST', '/admin/login/code', { code }),
  submitAdminWorkspace: (optionId) => request('POST', '/admin/login/workspace', { option_id: optionId }),
  cancelAdminLogin: () => request('POST', '/admin/login/cancel'),
  logoutAdmin: () => request('POST', '/admin/logout'),

  startSwapSeats: (maxChatgptActive = 2, accountId = '') => request('POST', '/tasks/swap-seats', {
    max_chatgpt_active: Math.max(1, Math.min(5, Number(maxChatgptActive) || 2)),
    account_id: accountId,
  }),
  startConsumePendingInvite: (email = '', accountId = '', maxChatgptActive = null) => request('POST', '/tasks/add', {
    email,
    account_id: accountId,
    ...(maxChatgptActive === null ? {} : { max_chatgpt_active: Math.max(1, Math.min(5, Number(maxChatgptActive) || 2)) }),
  }),
  startInviteAdd: (accountId = '', maxChatgptActive = null, forceCreateInvite = false, inviteDomains = '') => request('POST', '/tasks/invite-add', {
    account_id: accountId,
    force_create_invite: !!forceCreateInvite,
    ...(String(inviteDomains || '').trim() ? { invite_domains: String(inviteDomains || '').trim() } : {}),
    ...(maxChatgptActive === null ? {} : { max_chatgpt_active: Math.max(1, Math.min(5, Number(maxChatgptActive) || 2)) }),
  }),
  startClearPendingInvites: (accountId = '', concurrency = 4, confirm = false) => request('POST', '/tasks/invites/clear', {
    account_id: accountId,
    concurrency: Math.max(1, Math.min(8, Number(concurrency) || 4)),
    confirm: !!confirm,
  }),
  startBulkInvite: (accountId = '', emails = '', concurrency = 3, confirm = false) => request('POST', '/tasks/invites/bulk', {
    account_id: accountId,
    emails,
    concurrency: Math.max(1, Math.min(8, Number(concurrency) || 3)),
    batch_size: 5,
    seat_type: 'usage_based',
    resend_emails: true,
    confirm: !!confirm,
  }),
  startAutoDetectReplace: (email = '', accountId = '', replaceMode = '') => request('POST', '/tasks/auto-detect-replace', {
    email,
    account_id: accountId,
    ...(replaceMode ? { replace_mode: replaceMode } : {}),
  }),
  startManageTeams: (maxChatgptActive = 2, replaceWithPendingInvite = true, replaceMode = '') => request('POST', '/tasks/manage-teams', {
    max_chatgpt_active: Math.max(1, Math.min(5, Number(maxChatgptActive) || 2)),
    replace_with_pending_invite: !!replaceWithPendingInvite,
    ...(replaceMode ? { replace_mode: replaceMode } : {}),
  }),
  getTeams: () => request('GET', '/teams'),
  getSwapRuntimeStatus: () => request('GET', '/swap/runtime-status'),

  getTasks: () => request('GET', '/tasks'),
  getTask: (id) => request('GET', `/tasks/${id}`),

  getAutoCheckConfig: () => request('GET', '/config/auto-check'),
  setAutoCheckConfig: (cfg) => request('PUT', '/config/auto-check', cfg),

  getTeamMembers: (accountId = '', options = {}) => {
    const params = new URLSearchParams()
    if (accountId) params.set('account_id', accountId)
    if (options.includeInvites) params.set('include_invites', 'true')
    if (options.includeInviteCount === true) params.set('include_invite_count', 'true')
    if (options.includeInviteCount === false) params.set('include_invite_count', 'false')
    if (options.inviteLimit) params.set('invite_limit', String(options.inviteLimit))
    const query = params.toString()
    return request('GET', `/team/members${query ? `?${query}` : ''}`)
  },
  getLogs: (limit = 100, since = 0) => request('GET', `/logs?limit=${limit}&since=${since}`),
}
