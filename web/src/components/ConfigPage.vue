<template>
  <div class="mt-6 space-y-6">
    <div class="glass-card overflow-hidden p-6">
      <div class="pointer-events-none absolute"></div>
      <div class="flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div class="mb-3 inline-flex items-center gap-2 rounded-full border border-blue-400/20 bg-blue-500/10 px-4 py-2 text-sm text-blue-200">
            <span class="inline-block h-2 w-2 rounded-full bg-cyan-400 shadow-[0_0_14px_rgba(34,211,238,0.9)]"></span>
            AutoTeam Configuration Center
          </div>
          <h2 class="section-heading">配置面板</h2>
          <p class="section-subtitle max-w-2xl">
            围绕 swap_seat 配置 CPA、CFMail、多 Team、自动巡检和白名单；CPA 是 OAuth/auth 真相源，AutoTeam 只负责 quota 检查、seat 收敛和 pending invite 消费。
          </p>
        </div>

        <div class="status-badge max-w-sm text-xs leading-6 text-slate-400">
          高频配置前置，归档兼容项后置；页面不会提供 kick/remove/cancel invite 入口。
        </div>
      </div>

      <div class="mt-6 grid gap-4 md:grid-cols-3">
        <div class="glass-card-soft p-4">
          <div class="text-2xl">🧩</div>
          <div class="mt-3 text-sm font-medium text-white">swap_seat 专用</div>
            <div class="mt-1 text-xs leading-5 text-slate-400">只配置 quota 检查、seat/OAuth active 收敛、多 Team 和白名单。</div>
        </div>
        <div class="glass-card-soft p-4">
          <div class="text-2xl">🏢</div>
          <div class="mt-3 text-sm font-medium text-white">多 Team 调度</div>
          <div class="mt-1 text-xs leading-5 text-slate-400">每个 Team 独立 quota 缓存、冷却和 pending invite 消费。</div>
        </div>
        <div class="glass-card-soft p-4">
          <div class="text-2xl">🛡️</div>
          <div class="mt-3 text-sm font-medium text-white">安全边界固定</div>
          <div class="mt-1 text-xs leading-5 text-slate-400">只允许 swap seat 和 CPA auth enable/disable，不会移除成员或取消邀请。</div>
        </div>
      </div>
    </div>

    <div class="glass-card p-4">
      <div class="flex flex-wrap gap-2">
        <button
          v-for="item in visualCategories"
          :key="item.key"
          @click="visualCategory = item.key"
          class="pill-tab flex items-center gap-2"
          :class="visualCategory === item.key
            ? 'pill-tab-active'
            : ''"
        >
          <span class="text-base">{{ item.icon }}</span>
          {{ item.label }}
        </button>
      </div>
    </div>

    <div
      v-if="selectedRuntimeCategory"
      class="glass-card p-6"
    >
      <div class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div class="mb-2 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">
            <span>{{ currentRuntimeCategoryMeta?.icon }}</span>
            {{ currentRuntimeCategoryMeta?.badge }}
          </div>
          <h3 class="section-heading">{{ currentRuntimeCategoryMeta?.title }}</h3>
          <p class="section-subtitle max-w-3xl">
            {{ currentRuntimeCategoryMeta?.description }}
          </p>
          <p
            v-if="currentRuntimeCategoryMeta?.note"
            class="mt-2 text-xs text-slate-500"
          >
            {{ currentRuntimeCategoryMeta.note }}
          </p>
        </div>
        <div class="flex items-center gap-3">
          <span
            v-if="runtimeSaved"
            class="status-badge border-emerald-400/20 bg-emerald-500/10 text-emerald-200"
          >
            已保存
          </span>
          <span
            class="status-badge min-w-[84px] justify-center"
            :class="currentRuntimeStatus.class"
          >
            {{ currentRuntimeStatus.label }}
          </span>
        </div>
      </div>

      <div
        v-if="runtimeMessage"
        class="mb-4 rounded-2xl px-4 py-3 text-sm border"
        :class="runtimeMessageClass"
      >
        {{ runtimeMessage }}
      </div>

      <div v-if="runtimeLoading" class="text-sm text-slate-400">
        正在加载当前配置...
      </div>

      <div v-else-if="selectedRuntimeCategory === 'cloudmail'" class="space-y-5">
        <div class="rounded-2xl border border-white/10 bg-white/5 p-5">
          <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <div class="text-sm font-medium text-white">邮箱服务列表</div>
              <div class="mt-1 text-xs leading-5 text-slate-400">
                pending invite 替换只会使用 CFMail / Cloudflare Temp Email；可配置多个域名，支持随机子域名。CloudMail 仅作为旧环境兼容项保留。
              </div>
            </div>
            <div class="status-badge text-xs text-slate-400">
              {{ defaultMailService ? `默认：${mailServiceCardTitle(defaultMailService)}` : '未设置默认服务' }}
            </div>
          </div>

          <div class="mt-4 flex flex-wrap gap-3">
            <button class="btn-primary" @click="addMailService('cloudflare_temp_email')">
              + 添加 CFMail / Cloudflare Temp Email
            </button>
          </div>
          <details class="mt-3 rounded-xl border border-white/10 bg-slate-950/25 px-4 py-3">
            <summary class="cursor-pointer text-xs font-medium text-slate-300">
              归档兼容：CloudMail
            </summary>
            <p class="mt-2 text-xs leading-5 text-slate-500">
              新流程建议使用 CFMail / Cloudflare Temp Email 来消费已有 pending invite。CloudMail 只保留给旧环境兼容。
            </p>
            <button class="btn-secondary mt-3" @click="addMailService('cloudmail')">
              + 添加 CloudMail（兼容）
            </button>
          </details>
        </div>

        <div
          v-if="!mailServices.length"
          class="rounded-2xl border border-dashed border-white/10 bg-white/5 px-4 py-5 text-sm text-slate-400"
        >
          还没有配置任何邮箱服务。先添加一个服务，再设置为默认服务。
        </div>

        <div
          v-for="service in mailServices"
          :key="service.id"
          class="rounded-2xl border border-white/10 bg-white/5 p-5"
        >
          <div class="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <div class="flex flex-wrap items-center gap-2">
                <div class="text-sm font-medium text-white">{{ mailServiceCardTitle(service) }}</div>
                <span class="status-badge text-[11px] text-slate-300">
                  {{ mailServiceTypeLabel(service.type) }}
                </span>
                <span
                  v-if="mailServiceDefault === service.id"
                  class="status-badge border-emerald-400/20 bg-emerald-500/10 text-[11px] text-emerald-200"
                >
                  默认新建服务
                </span>
                <span
                  v-if="!isMailServiceComplete(service)"
                  class="status-badge border-amber-400/20 bg-amber-500/10 text-[11px] text-amber-200"
                >
                  待补全
                </span>
              </div>
              <div class="mt-1 text-xs leading-5 text-slate-400">
                {{ mailServiceDescription(service.type) }}
              </div>
            </div>

            <div class="flex flex-wrap gap-2">
              <button
                v-if="mailServiceDefault !== service.id"
                class="btn-secondary"
                @click="setDefaultMailService(service.id)"
              >
                设为默认
              </button>
              <button
                class="btn-secondary border-red-500/30 text-red-300 hover:border-red-400/40 hover:text-red-200"
                @click="removeMailService(service.id)"
              >
                删除
              </button>
            </div>
          </div>

          <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div class="rounded-2xl border border-white/10 bg-slate-950/25 p-4">
              <label class="mb-2 block text-sm font-medium text-slate-300">
                服务名称
                <span class="ml-1 text-xs font-normal text-slate-500">（可选）</span>
              </label>
              <input
                v-model="service.name"
                type="text"
                placeholder="例如：CloudMail #1"
                class="input-dark"
              />
            </div>

            <div
              v-for="field in mailServiceFields(service)"
              :key="`${service.id}-${field.key}`"
              class="rounded-2xl border border-white/10 bg-slate-950/25 p-4"
            >
              <label class="mb-2 block text-sm font-medium text-slate-300">
                {{ field.label }}
                <span v-if="field.required" class="text-red-400">*</span>
                <div v-if="field.hint" class="mt-1 text-[11px] font-normal text-slate-500 break-all">
                  {{ field.hint }}
                </div>
              </label>
              <input
                v-model="service[field.key]"
                :type="field.inputType || 'text'"
                :placeholder="field.placeholder || ''"
                class="input-dark"
              />
            </div>
          </div>
        </div>

        <div class="flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/5 p-4 lg:flex-row lg:items-center lg:justify-between">
          <p class="text-xs leading-6 text-slate-400">
            保存后会立即热加载。消费 pending invite 时会按邮箱域名匹配对应 CFMail 服务；无法匹配时使用默认服务。
          </p>
          <button
            @click="saveRuntimeConfig"
            :disabled="runtimeSaving || runtimeLoading"
            class="btn-primary"
          >
            {{ runtimeSaving ? '保存中...' : '保存配置' }}
          </button>
        </div>
      </div>

      <div v-else-if="selectedRuntimeCategory === 'cpa'" class="space-y-5">
        <div class="rounded-2xl border border-white/10 bg-white/5 p-5">
          <div class="mb-4 flex items-center justify-between gap-4">
            <div>
              <div class="text-sm font-medium text-white">CPA 管理端</div>
              <div class="mt-1 text-xs leading-5 text-slate-400">
                AutoTeam 只调用 CPA API 读取 auth-files、检查 5h/weekly quota，并启用/禁用 OAuth active。
              </div>
            </div>
            <div class="status-badge text-xs text-slate-400">
              {{ cpaStatusText }}
            </div>
          </div>
          <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div v-for="field in cpaFields" :key="field.key" class="rounded-2xl border border-white/10 bg-slate-950/25 p-4">
              <label class="mb-2 block text-sm font-medium text-slate-300">
                {{ field.prompt }}
                <span v-if="isRuntimeRequired(field)" class="text-red-400">*</span>
              </label>
              <input
                v-model="runtimeForm[field.key]"
                :type="fieldInputType(field.key)"
                :placeholder="field.default || ''"
                class="input-dark"
              />
            </div>
          </div>
        </div>

        <details v-if="cpaArchivedFields.length" class="rounded-2xl border border-white/10 bg-white/5 p-5">
          <summary class="cursor-pointer text-sm font-medium text-slate-300">
            归档兼容设置（Sub2API / 旧同步开关）
          </summary>
          <p class="mt-2 text-xs leading-5 text-slate-500">
            swap_seat 主流程不会依赖这些字段；仅保留给旧环境迁移或排查使用。
          </p>
          <div class="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div v-for="field in cpaArchivedFields" :key="field.key" class="rounded-2xl border border-white/10 bg-slate-950/25 p-4">
              <label class="mb-2 block text-sm font-medium text-slate-300">
                {{ field.prompt }}
                <div v-if="sub2apiFieldHint(field.key)" class="mt-1 font-mono text-[11px] font-normal text-slate-500 break-all">
                  {{ sub2apiFieldHint(field.key) }}
                </div>
              </label>
              <select
                v-if="isBooleanStringField(field.key)"
                v-model="runtimeForm[field.key]"
                class="input-dark"
              >
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
              <select
                v-else-if="isWsModeField(field.key)"
                v-model="runtimeForm[field.key]"
                class="input-dark"
              >
                <option value="off">off</option>
                <option value="ctx_pool">ctx_pool</option>
                <option value="passthrough">passthrough</option>
              </select>
              <input
                v-else
                v-model="runtimeForm[field.key]"
                :type="fieldInputType(field.key)"
                :step="fieldInputStep(field.key)"
                :placeholder="field.default || ''"
                class="input-dark"
              />
            </div>
          </div>
        </details>

        <div class="flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/5 p-4 lg:flex-row lg:items-center lg:justify-between">
          <p class="text-xs leading-6 text-slate-400">
            保存后会立即热加载；填写 CPA_URL / CPA_KEY 后即可运行多 Team quota 检查与 seat/OAuth 收敛。
          </p>
          <button
            @click="saveRuntimeConfig"
            :disabled="runtimeSaving || runtimeLoading"
            class="btn-primary"
          >
            {{ runtimeSaving ? '保存中...' : '保存配置' }}
          </button>
        </div>
      </div>

      <div v-else-if="selectedRuntimeCategory === 'proxy'" class="space-y-4">
        <div class="rounded-2xl border border-white/10 bg-white/5 p-4">
          <button
            @click="proxyExpanded = !proxyExpanded"
            class="flex w-full items-center justify-between gap-4 text-left"
          >
            <div>
              <div class="text-sm font-medium text-white">高级代理设置</div>
              <div class="mt-1 text-xs leading-5 text-slate-400">
                低频配置，默认折叠。只有浏览器流量需要单独代理时才建议填写。
              </div>
            </div>
            <span class="text-xs text-slate-400">{{ proxyExpanded ? '收起' : '展开' }}</span>
          </button>

          <div v-if="proxyExpanded" class="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div v-for="field in proxyFields" :key="field.key" class="rounded-2xl border border-white/10 bg-slate-950/25 p-4">
              <label class="mb-2 block text-sm font-medium text-slate-300">
                {{ field.prompt }}
                <span v-if="isRuntimeRequired(field)" class="text-red-400">*</span>
              </label>
              <input
                v-model="runtimeForm[field.key]"
                :type="fieldInputType(field.key)"
                :placeholder="field.default || ''"
                class="input-dark"
              />
            </div>
          </div>
        </div>

        <div class="flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/5 p-4 lg:flex-row lg:items-center lg:justify-between">
          <p class="text-xs leading-6 text-slate-400">
            推荐只在确实需要代理 Playwright 浏览器流量时启用，并配合绕过列表避免本地回调误走代理。
          </p>
          <button
            @click="saveRuntimeConfig"
            :disabled="runtimeSaving || runtimeLoading"
            class="btn-primary"
          >
            {{ runtimeSaving ? '保存中...' : '保存配置' }}
          </button>
        </div>
      </div>

      <div v-else-if="selectedRuntimeCategory === 'teams'" class="space-y-5">
        <div class="rounded-2xl border border-white/10 bg-white/5 p-5">
          <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <div class="text-sm font-medium text-white">受管 Team 列表</div>
              <div class="mt-1 text-xs leading-5 text-slate-400">
                每个 Team 可单独设置 ChatGPT/OAuth active 保留数（1~5）和指定 pending invite。留空则只管理当前管理员 session 的默认 Team。
              </div>
            </div>
            <div class="flex flex-wrap gap-2">
              <button class="btn-secondary" @click="addTeamRow">
                + 添加 Team
              </button>
              <button class="btn-secondary" @click="reloadTeamRowsFromJson">
                从 JSON 重新载入
              </button>
            </div>
          </div>

          <div v-if="teamJsonError" class="mt-4 rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
            {{ teamJsonError }}
          </div>

          <div v-if="!teamRows.length" class="mt-4 rounded-2xl border border-dashed border-white/10 bg-slate-950/25 px-4 py-6 text-sm text-slate-400">
            当前未配置多 Team。系统会回退当前管理员登录态绑定的 Team。需要同时管理多个 Team 时，点击「添加 Team」。
          </div>

          <div v-else class="mt-4 grid gap-4 xl:grid-cols-2">
            <div
              v-for="(team, index) in teamRows"
              :key="team._key"
              class="rounded-2xl border border-white/10 bg-slate-950/25 p-4"
            >
              <div class="mb-4 flex items-start justify-between gap-3">
                <div>
                  <div class="text-sm font-medium text-white">
                    {{ team.workspace_name || team.id || team.account_id || `Team #${index + 1}` }}
                  </div>
                  <div class="mt-1 text-xs text-slate-500">
                    account_id 是 Team 调度和冷却隔离的主键；不要填个人账号邮箱。
                  </div>
                </div>
                <button
                  class="btn-secondary border-red-500/30 text-red-300 hover:border-red-400/40 hover:text-red-200"
                  @click="removeTeamRow(index)"
                >
                  删除
                </button>
              </div>

              <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">启用调度</span>
                  <select v-model="team.enabled" class="input-dark">
                    <option :value="true">启用</option>
                    <option :value="false">停用</option>
                  </select>
                </label>

                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">ChatGPT/OAuth active 保留数</span>
                  <input v-model.number="team.max_chatgpt_active" type="number" min="1" max="5" class="input-dark" />
                </label>

                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">
                    account_id <span class="text-red-400">*</span>
                  </span>
                  <input v-model.trim="team.account_id" type="text" placeholder="Team / workspace account_id" class="input-dark font-mono text-xs" />
                  <span v-if="teamHasAnyValue(team) && !team.account_id" class="mt-2 block text-xs text-red-300">此项必填，否则保存会失败。</span>
                </label>

                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">显示名称</span>
                  <input v-model.trim="team.workspace_name" type="text" placeholder="例如 Team A" class="input-dark" />
                </label>

                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">配置 ID（可选）</span>
                  <input v-model.trim="team.id" type="text" placeholder="team-a" class="input-dark" />
                </label>

                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">指定 pending invite 邮箱（可选）</span>
                  <input v-model.trim="team.pending_invite_email" type="email" placeholder="pending@example.com" class="input-dark" />
                </label>

                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">管理员邮箱（可选）</span>
                  <input v-model.trim="team.email" type="email" placeholder="默认继承当前管理员邮箱" class="input-dark" />
                </label>

                <label class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <span class="mb-2 block text-sm font-medium text-slate-300">独立 session_token（可选）</span>
                  <input v-model.trim="team.session_token" type="password" placeholder="留空则共享默认管理员 session" class="input-dark font-mono text-xs" />
                </label>
              </div>
            </div>
          </div>
        </div>

        <details class="rounded-2xl border border-white/10 bg-white/5 p-5">
          <summary class="cursor-pointer text-sm font-medium text-slate-300">
            JSON 源码（自动与上方表单同步）
          </summary>
          <textarea
            v-model="runtimeForm.TEAM_WORKSPACES_JSON"
            rows="10"
            spellcheck="false"
            :placeholder="teamJsonPlaceholder"
            class="input-dark mt-4 font-mono text-xs"
          ></textarea>
        </details>

        <div class="flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/5 p-4 lg:flex-row lg:items-center lg:justify-between">
          <p class="text-xs leading-6 text-slate-400">
            保存后会立即热加载。多 Team 的 quota 缓存与 swap 冷却会按 account_id 隔离；disabled Team 不会参与自动调度。
          </p>
          <button
            @click="saveRuntimeConfig"
            :disabled="runtimeSaving || runtimeLoading"
            class="btn-primary"
          >
            {{ runtimeSaving ? '保存中...' : '保存 Team 配置' }}
          </button>
        </div>
      </div>

      <div v-else class="space-y-4">
        <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          <div v-for="field in currentRuntimeFields" :key="field.key" class="rounded-2xl border border-white/10 bg-white/5 p-4">
            <label class="mb-2 block text-sm font-medium text-slate-300">
              {{ field.prompt }}
              <span v-if="isRuntimeRequired(field)" class="text-red-400">*</span>
              <span v-if="field.key === 'API_KEY'" class="ml-1 text-xs text-slate-500">（留空自动生成）</span>
            </label>
            <textarea
              v-if="fieldUsesTextarea(field.key)"
              v-model="runtimeForm[field.key]"
              rows="8"
              spellcheck="false"
              :placeholder="teamJsonPlaceholder"
              class="input-dark font-mono text-xs"
            ></textarea>
            <input
              v-else
              v-model="runtimeForm[field.key]"
              :type="fieldInputType(field.key)"
              :placeholder="field.default || ''"
              class="input-dark"
            />
          </div>
        </div>

        <div class="flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/5 p-4 lg:flex-row lg:items-center lg:justify-between">
          <p class="text-xs leading-6 text-slate-400">
            {{ currentRuntimeCategoryMeta?.footer }}
          </p>
          <button
            @click="saveRuntimeConfig"
            :disabled="runtimeSaving || runtimeLoading"
            class="btn-primary"
          >
            {{ runtimeSaving ? '保存中...' : '保存配置' }}
          </button>
        </div>
      </div>
    </div>

    <Settings
      v-else-if="visualCategory === 'admin'"
      :admin-status="adminStatus"
      :codex-status="codexStatus"
      section="admin"
      @refresh="$emit('refresh')"
      @admin-progress="$emit('admin-progress')"
    />

    <Settings
      v-else-if="visualCategory === 'auto-check'"
      :admin-status="adminStatus"
      :codex-status="codexStatus"
      section="auto-check"
      @refresh="$emit('refresh')"
      @admin-progress="$emit('admin-progress')"
    />

    <div v-else-if="visualCategory === 'source'" class="glass-card space-y-4 p-6">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div class="mb-2 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">
            <span>📝</span>
            Source Editor
          </div>
          <h3 class="section-heading">源文件编辑</h3>
          <p class="section-subtitle">
            直接编辑 .env 源文件。保存后会立即重载并校验 CPA、邮箱服务和多 Team 配置。
          </p>
        </div>
        <div class="status-badge break-all font-mono text-[11px] text-slate-400">
          {{ sourcePath || '.env' }}
        </div>
      </div>

      <div
        v-if="sourceMessage"
        class="rounded-2xl px-4 py-3 text-sm border"
        :class="sourceMessageClass"
      >
        {{ sourceMessage }}
      </div>

      <textarea
        v-model="sourceContent"
        rows="20"
        spellcheck="false"
        class="textarea-dark min-h-[420px] font-mono"
        placeholder="在这里编辑 .env 内容"
      />

      <div class="flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/5 p-4 lg:flex-row lg:items-center lg:justify-between">
        <p class="text-xs leading-6 text-slate-400">
          这里是原始文本模式，适合你直接粘贴或手工维护完整 .env。
        </p>
        <div class="flex gap-2">
          <button
            @click="loadSourceConfig"
            :disabled="sourceLoading || sourceSaving"
            class="btn-secondary"
          >
            {{ sourceLoading ? '加载中...' : '重新读取' }}
          </button>
          <button
            @click="saveSourceConfig"
            :disabled="sourceLoading || sourceSaving"
            class="btn-primary"
          >
            {{ sourceSaving ? '保存中...' : '保存源文件' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api, setApiKey } from '../api.js'
import Settings from './Settings.vue'

defineProps({
  adminStatus: {
    type: Object,
    default: null,
  },
  codexStatus: {
    type: Object,
    default: null,
  },
})

const emit = defineEmits(['refresh', 'admin-progress'])

const runtimeCategoryKeys = {
  cloudmail: ['MAIL_PROVIDER', 'CLOUDMAIL_BASE_URL', 'CLOUDMAIL_EMAIL', 'CLOUDMAIL_PASSWORD', 'CLOUDMAIL_DOMAIN', 'CF_TEMP_EMAIL_BASE_URL', 'CF_TEMP_EMAIL_ADMIN_PASSWORD', 'CF_TEMP_EMAIL_DOMAIN'],
  cpa: [
    'SYNC_TARGET_CPA',
    'SYNC_TARGET_SUB2API',
    'CPA_URL',
    'CPA_KEY',
    'SUB2API_URL',
    'SUB2API_EMAIL',
    'SUB2API_PASSWORD',
    'SUB2API_GROUP',
    'SUB2API_CONCURRENCY',
    'SUB2API_PRIORITY',
    'SUB2API_RATE_MULTIPLIER',
    'SUB2API_AUTO_PAUSE_ON_EXPIRED',
    'SUB2API_MODEL_WHITELIST',
    'SUB2API_OPENAI_WS_MODE',
    'SUB2API_OPENAI_PASSTHROUGH',
    'SUB2API_OVERWRITE_ACCOUNT_SETTINGS',
    'SUB2API_PROXY',
  ],
  proxy: ['PLAYWRIGHT_PROXY_URL', 'PLAYWRIGHT_PROXY_BYPASS'],
  security: ['API_KEY', 'SWAP_SEAT_WHITELIST_EMAILS'],
  teams: ['TEAM_WORKSPACES_JSON'],
}

const runtimeCategoryMeta = {
  cloudmail: {
    icon: '📧',
    badge: 'Mail Services',
    title: 'CFMail / 邮箱服务',
    description: '配置消费 pending invite 时用来读取邀请邮件和验证码的 CFMail / Cloudflare Temp Email 服务。',
    note: '支持多个服务和多域名；xxxxx.a.com、*.a.com、{random}.a.com 会在创建地址时启用随机子域名。CloudMail 仅保留兼容。',
    footer: '邮箱配置保存后会立即热加载；pending invite 注册会按邮箱域名优先匹配对应服务。',
  },
  cpa: {
    icon: '☁️',
    badge: 'CPA Control',
    title: 'CPA 管理端',
    description: '配置 CPA_URL / CPA_KEY。AutoTeam 通过 CPA 读取 OAuth/auth-files、检查 quota，并启停 OAuth active/disabled。',
    note: 'Sub2API / 旧同步字段已归档到折叠区；swap_seat 主流程不再维护本地账号池同步。',
  },
  proxy: {
    icon: '🛰️',
    badge: 'Proxy / Advanced',
    title: '代理 / 高级',
    description: '用于单独配置 Playwright 浏览器流量代理。属于低频项，默认折叠，避免把主配置界面堆得过满。',
    note: '只有在代理 ChatGPT / Auth 页面访问时才建议配置；本地回调场景通常还需要设置 bypass。',
  },
  security: {
    icon: '🔐',
    badge: 'Security',
    title: '安全 / 白名单',
    description: '入口级 API Key 与 swap_seat 白名单集中放在这里。白名单成员不检查 quota、不切 seat、不启停 CPA OAuth。',
    note: 'API Key 留空会自动生成新的密钥；SWAP_SEAT_WHITELIST_EMAILS 支持逗号、分号、空格或换行分隔。',
    footer: '保存后会立即生效。修改 API Key 会同步刷新当前浏览器里的访问密钥。',
  },
  teams: {
    icon: '🏢',
    badge: 'Multi Team',
    title: '多 Team 管理',
    description: '配置多个 Team workspace 后，系统会逐个 Team 独立检查 quota、独立 swap_seat；该 Team 全员 quota 耗尽时才消费该 Team 的 pending invite。',
    note: 'TEAM_WORKSPACES_JSON 支持 JSON 数组；每项至少需要 account_id，可选 workspace_name、session_token、email、max_chatgpt_active、pending_invite_email。',
    footer: '留空时只管理当前管理员登录态绑定的 Team。多 Team 的冷却和 quota 缓存按 account_id 隔离，不会互相占用。',
  },
}

const visualCategories = [
  { key: 'cloudmail', label: 'CFMail', icon: '📧' },
  { key: 'cpa', label: 'CPA 管理', icon: '☁️' },
  { key: 'security', label: '安全 / 访问控制', icon: '🔐' },
  { key: 'teams', label: '多 Team', icon: '🏢' },
  { key: 'admin', label: '管理员 / 主号', icon: '👤' },
  { key: 'auto-check', label: '巡检设置', icon: '🔄' },
  { key: 'source', label: '源文件编辑', icon: '📝' },
  { key: 'proxy', label: '代理 / 高级', icon: '🛰️' },
]

const visualCategory = ref('cloudmail')
const proxyExpanded = ref(false)

const runtimeFields = ref([])
const runtimeForm = reactive({})
const mailServices = ref([])
const mailServiceDefault = ref('')
const runtimeLoading = ref(false)
const runtimeSaving = ref(false)
const runtimeSaved = ref(false)
const runtimeMessage = ref('')
const runtimeMessageClass = ref('')

const sourcePath = ref('')
const sourceContent = ref('')
const sourceLoading = ref(false)
const sourceSaving = ref(false)
const sourceLoaded = ref(false)
const sourceMessage = ref('')
const sourceMessageClass = ref('')
const teamRows = ref([])
const teamJsonError = ref('')
let teamRowCounter = 0
let syncingTeamJsonFromRows = false
const runtimeRequiredKeys = new Set(['API_KEY'])
const sub2apiFieldHints = {
  SUB2API_URL: 'ENV: SUB2API_URL · Sub2API API base URL',
  SUB2API_EMAIL: 'ENV: SUB2API_EMAIL · login.email',
  SUB2API_PASSWORD: 'ENV: SUB2API_PASSWORD · login.password',
  SUB2API_GROUP: 'ENV: SUB2API_GROUP · group_ids',
  SUB2API_PROXY: 'ENV: SUB2API_PROXY · 旧账号池兼容字段，swap_seat 主流程不读取',
  SUB2API_CONCURRENCY: 'ENV: SUB2API_CONCURRENCY · account.concurrency',
  SUB2API_PRIORITY: 'ENV: SUB2API_PRIORITY · account.priority',
  SUB2API_RATE_MULTIPLIER: 'ENV: SUB2API_RATE_MULTIPLIER · account.rate_multiplier',
  SUB2API_AUTO_PAUSE_ON_EXPIRED: 'ENV: SUB2API_AUTO_PAUSE_ON_EXPIRED · account.auto_pause_on_expired',
  SUB2API_MODEL_WHITELIST: 'ENV: SUB2API_MODEL_WHITELIST · credentials.model_mapping',
  SUB2API_OPENAI_WS_MODE: 'ENV: SUB2API_OPENAI_WS_MODE · extra.openai_oauth_responses_websockets_v2_mode / enabled',
  SUB2API_OPENAI_PASSTHROUGH: 'ENV: SUB2API_OPENAI_PASSTHROUGH · extra.openai_passthrough',
  SUB2API_OVERWRITE_ACCOUNT_SETTINGS: 'ENV: SUB2API_OVERWRITE_ACCOUNT_SETTINGS · AutoTeam overwrite switch',
}

const mailServiceFieldMeta = {
  cloudmail: [
    {
      key: 'base_url',
      label: 'CloudMail API 地址',
      required: true,
      placeholder: 'https://your-cloudmail.com/api',
    },
    {
      key: 'email',
      label: 'CloudMail 登录邮箱',
      required: true,
      placeholder: 'admin@example.com',
    },
    {
      key: 'password',
      label: 'CloudMail 登录密码',
      required: true,
      inputType: 'password',
    },
    {
      key: 'domain',
      label: 'CloudMail 邮箱域名',
      required: true,
      placeholder: 'example.com 或 @example.com',
      hint: '用于自动匹配已有账号所属邮箱服务',
    },
  ],
  cloudflare_temp_email: [
    {
      key: 'base_url',
      label: 'Cloudflare Temp Email API 地址',
      required: true,
      placeholder: 'https://temp-email-api.example.com',
    },
    {
      key: 'admin_password',
      label: '管理员密码',
      required: true,
      inputType: 'password',
    },
    {
      key: 'domain',
      label: '邮箱域名',
      required: true,
      placeholder: 'xxxxx.a.com; xxxxx.b.com 或 *.a.com; *.b.com',
      hint: '支持分号/逗号配置多域名；xxxxx.a.com、*.a.com、{random}.a.com 会为每次注册启用随机子域名。',
    },
  ],
}

const selectedRuntimeCategory = computed(() => runtimeCategoryKeys[visualCategory.value] ? visualCategory.value : '')
const currentRuntimeCategoryMeta = computed(() => runtimeCategoryMeta[selectedRuntimeCategory.value] || null)

function fieldByKey(key) {
  return runtimeFields.value.find(field => field.key === key) || null
}

function sub2apiFieldHint(key) {
  return sub2apiFieldHints[key] || ''
}

function fieldsByKeys(keys) {
  return keys
    .map(key => fieldByKey(key))
    .filter(Boolean)
}

const securityFields = computed(() => fieldsByKeys(runtimeCategoryKeys.security))
const proxyFields = computed(() => fieldsByKeys(runtimeCategoryKeys.proxy))
const teamFields = computed(() => fieldsByKeys(runtimeCategoryKeys.teams))
const defaultMailService = computed(() => mailServices.value.find(service => service.id === mailServiceDefault.value) || null)

const cpaFields = computed(() => fieldsByKeys(['CPA_URL', 'CPA_KEY']))
const cpaArchivedFields = computed(() => fieldsByKeys([
      'SYNC_TARGET_CPA',
      'SYNC_TARGET_SUB2API',
      'SUB2API_URL',
      'SUB2API_EMAIL',
      'SUB2API_PASSWORD',
      'SUB2API_GROUP',
      'SUB2API_CONCURRENCY',
      'SUB2API_PRIORITY',
      'SUB2API_RATE_MULTIPLIER',
      'SUB2API_AUTO_PAUSE_ON_EXPIRED',
      'SUB2API_MODEL_WHITELIST',
      'SUB2API_OPENAI_WS_MODE',
      'SUB2API_OPENAI_PASSTHROUGH',
      'SUB2API_OVERWRITE_ACCOUNT_SETTINGS',
      'SUB2API_PROXY',
    ]))

const currentRuntimeFields = computed(() => {
  if (selectedRuntimeCategory.value === 'security') {
    return securityFields.value
  }
  if (selectedRuntimeCategory.value === 'teams') {
    return teamFields.value
  }
  return []
})

const cpaStatusText = computed(() => {
  const hasUrl = String(runtimeForm.CPA_URL || '').trim()
  const hasKey = String(runtimeForm.CPA_KEY || '').trim()
  return hasUrl && hasKey ? 'CPA 已配置' : 'CPA 待配置'
})

const currentRuntimeStatus = computed(() => {
  if (!selectedRuntimeCategory.value) {
    return {
      label: '',
      class: 'border-white/10 bg-white/5 text-slate-400',
    }
  }

  if (selectedRuntimeCategory.value === 'cpa') {
    const cpaReady = cpaFields.value.every(field => !isRuntimeRequired(field) || field.configured)
    return cpaReady
      ? {
          label: '已配置',
          class: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200',
        }
      : {
          label: '未配置',
          class: 'border-red-400/20 bg-red-500/10 text-red-200',
        }
  }

  if (selectedRuntimeCategory.value === 'proxy') {
    return proxyFields.value.some(field => field.configured)
      ? {
          label: '已设置',
          class: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200',
        }
      : {
          label: '未设置',
          class: 'border-white/10 bg-white/5 text-slate-400',
        }
  }

  if (selectedRuntimeCategory.value === 'cloudmail') {
    if (!mailServices.value.length) {
      return {
        label: '未配置',
        class: 'border-red-400/20 bg-red-500/10 text-red-200',
      }
    }
    if (!defaultMailService.value) {
      return {
        label: '未设默认',
        class: 'border-amber-400/20 bg-amber-500/10 text-amber-200',
      }
    }
    if (mailServices.value.some(service => !isMailServiceComplete(service))) {
      return {
        label: '待补全',
        class: 'border-amber-400/20 bg-amber-500/10 text-amber-200',
      }
    }
    return {
      label: '已配置',
      class: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200',
    }
  }

  if (selectedRuntimeCategory.value === 'teams') {
    return String(runtimeForm.TEAM_WORKSPACES_JSON || '').trim()
      ? {
          label: '多 Team',
          class: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200',
        }
      : {
          label: '单 Team',
          class: 'border-white/10 bg-white/5 text-slate-400',
        }
  }

  const fields = currentRuntimeFields.value
  const configured = fields.length > 0 && fields.every(field => !isRuntimeRequired(field) || field.configured)

  return configured
    ? {
        label: '已配置',
        class: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200',
      }
    : {
        label: '未配置',
        class: 'border-red-400/20 bg-red-500/10 text-red-200',
      }
})

function setRuntimeMessage(text, type = 'success') {
  runtimeMessage.value = text
  runtimeMessageClass.value = type === 'success'
    ? 'bg-green-500/10 text-green-400 border-green-500/20'
    : 'bg-red-500/10 text-red-400 border-red-500/20'
  window.clearTimeout(setRuntimeMessage._timer)
  setRuntimeMessage._timer = window.setTimeout(() => {
    runtimeMessage.value = ''
  }, 8000)
}

function setSourceMessage(text, type = 'success') {
  sourceMessage.value = text
  sourceMessageClass.value = type === 'success'
    ? 'bg-green-500/10 text-green-400 border-green-500/20'
    : 'bg-red-500/10 text-red-400 border-red-500/20'
  window.clearTimeout(setSourceMessage._timer)
  setSourceMessage._timer = window.setTimeout(() => {
    sourceMessage.value = ''
  }, 8000)
}

function fieldInputType(key) {
  if (['SUB2API_CONCURRENCY', 'SUB2API_PRIORITY', 'SUB2API_RATE_MULTIPLIER'].includes(key)) {
    return 'number'
  }
  return key.includes('PASSWORD') || key.includes('KEY') ? 'password' : 'text'
}

const teamJsonPlaceholder = `[
  {
    "id": "team-a",
    "account_id": "00000000-0000-0000-0000-000000000000",
    "workspace_name": "Team A",
    "max_chatgpt_active": 2
  },
  {
    "id": "team-b",
    "account_id": "11111111-1111-1111-1111-111111111111",
    "workspace_name": "Team B",
    "max_chatgpt_active": 1,
    "pending_invite_email": "optional@example.com"
  }
]`

function clampTeamActiveLimit(value) {
  const n = Number.parseInt(value, 10)
  if (Number.isNaN(n)) return 2
  return Math.max(1, Math.min(5, n))
}

function createTeamRow(source = {}) {
  teamRowCounter += 1
  return {
    _key: `team-${Date.now().toString(36)}-${teamRowCounter}`,
    id: String(source.id || source.team_id || source.teamId || source.key || ''),
    account_id: String(source.account_id || source.accountId || source.account || source.id || ''),
    workspace_name: String(source.workspace_name || source.workspaceName || source.name || ''),
    email: String(source.email || ''),
    session_token: String(source.session_token || source.sessionToken || ''),
    enabled: source.enabled === undefined ? true : Boolean(source.enabled),
    max_chatgpt_active: clampTeamActiveLimit(source.max_chatgpt_active ?? source.target_seats ?? 2),
    pending_invite_email: String(source.pending_invite_email || source.pendingInviteEmail || ''),
  }
}

function teamHasAnyValue(team) {
  return Boolean(
    String(team?.id || '').trim() ||
    String(team?.account_id || '').trim() ||
    String(team?.workspace_name || '').trim() ||
    String(team?.email || '').trim() ||
    String(team?.session_token || '').trim() ||
    String(team?.pending_invite_email || '').trim()
  )
}

function parseTeamRowsFromJson(value) {
  const text = String(value || '').trim()
  if (!text) return []
  const parsed = JSON.parse(text)
  const rows = Array.isArray(parsed)
    ? parsed
    : parsed?.teams || parsed?.workspaces || parsed?.items || []
  if (!Array.isArray(rows)) {
    throw new Error('TEAM_WORKSPACES_JSON 必须是数组，或包含 teams/workspaces/items 数组')
  }
  return rows.filter(item => item && typeof item === 'object').map(item => createTeamRow(item))
}

function loadTeamRowsFromJson(showMessage = false) {
  if (syncingTeamJsonFromRows) return
  try {
    teamRows.value = parseTeamRowsFromJson(runtimeForm.TEAM_WORKSPACES_JSON)
    teamJsonError.value = ''
    if (showMessage) {
      setRuntimeMessage(teamRows.value.length ? `已载入 ${teamRows.value.length} 个 Team` : '已清空多 Team 配置')
    }
  } catch (e) {
    teamJsonError.value = e.message
  }
}

function teamRowsPayload() {
  return teamRows.value
    .filter(teamHasAnyValue)
    .map(team => {
      const item = {
        account_id: String(team.account_id || '').trim(),
        max_chatgpt_active: clampTeamActiveLimit(team.max_chatgpt_active),
      }
      const id = String(team.id || '').trim()
      const workspaceName = String(team.workspace_name || '').trim()
      const email = String(team.email || '').trim().toLowerCase()
      const sessionToken = String(team.session_token || '').trim()
      const pendingEmail = String(team.pending_invite_email || '').trim().toLowerCase()
      if (id) item.id = id
      if (workspaceName) item.workspace_name = workspaceName
      if (team.enabled === false) item.enabled = false
      if (email) item.email = email
      if (sessionToken) item.session_token = sessionToken
      if (pendingEmail) item.pending_invite_email = pendingEmail
      return item
    })
}

function syncTeamRowsToJson() {
  const rows = teamRowsPayload()
  syncingTeamJsonFromRows = true
  runtimeForm.TEAM_WORKSPACES_JSON = rows.length ? JSON.stringify(rows, null, 2) : ''
  teamJsonError.value = ''
  window.setTimeout(() => {
    syncingTeamJsonFromRows = false
  }, 0)
}

function addTeamRow() {
  teamRows.value = [...teamRows.value, createTeamRow({ max_chatgpt_active: 2 })]
}

function removeTeamRow(index) {
  teamRows.value = teamRows.value.filter((_, i) => i !== index)
}

function reloadTeamRowsFromJson() {
  loadTeamRowsFromJson(true)
}

function fieldUsesTextarea(key) {
  return key === 'TEAM_WORKSPACES_JSON'
}

function isToggleField(key) {
  return key === 'SYNC_TARGET_CPA' || key === 'SYNC_TARGET_SUB2API'
}

function isBooleanStringField(key) {
  return isToggleField(key) || [
    'SUB2API_AUTO_PAUSE_ON_EXPIRED',
    'SUB2API_OPENAI_PASSTHROUGH',
    'SUB2API_OVERWRITE_ACCOUNT_SETTINGS',
  ].includes(key)
}

function isWsModeField(key) {
  return key === 'SUB2API_OPENAI_WS_MODE'
}

function fieldInputStep(key) {
  if (key === 'SUB2API_RATE_MULTIPLIER') {
    return '0.001'
  }
  if (key === 'SUB2API_CONCURRENCY' || key === 'SUB2API_PRIORITY') {
    return '1'
  }
  return undefined
}

function createMailService(type = 'cloudflare_temp_email') {
  const normalizedType = String(type || '').toLowerCase() === 'cloudflare_temp_email'
    ? 'cloudflare_temp_email'
    : 'cloudmail'
  const randomPart = Math.random().toString(36).slice(2, 8)
  return {
    id: `mailsvc-${Date.now().toString(36)}-${randomPart}`,
    type: normalizedType,
    name: '',
    base_url: '',
    domain: '',
    email: '',
    password: '',
    admin_password: '',
  }
}

function normalizeMailService(service) {
  const template = createMailService(service?.type)
  return {
    ...template,
    id: String(service?.id || template.id),
    name: String(service?.name || ''),
    base_url: String(service?.base_url || ''),
    domain: String(service?.domain || ''),
    email: String(service?.email || ''),
    password: String(service?.password || ''),
    admin_password: String(service?.admin_password || ''),
  }
}

function sanitizeMailService(service) {
  const normalized = normalizeMailService(service)
  normalized.domain = normalized.domain.trim()
  if (normalized.type === 'cloudflare_temp_email') {
    delete normalized.email
    delete normalized.password
  } else {
    delete normalized.admin_password
  }
  return normalized
}

function mailServiceTypeLabel(type) {
  return type === 'cloudflare_temp_email' ? 'Cloudflare Temp Email' : 'CloudMail'
}

function mailServiceDescription(type) {
  return type === 'cloudflare_temp_email'
    ? '填写管理端 API 地址、管理员密码和对应邮箱域名。'
    : '填写 CloudMail API 地址、管理员账号密码和对应邮箱域名。'
}

function mailServiceFields(service) {
  return mailServiceFieldMeta[service?.type] || mailServiceFieldMeta.cloudmail
}

function isMailServiceComplete(service) {
  return mailServiceFields(service).every(field => {
    if (!field.required) {
      return true
    }
    return String(service?.[field.key] || '').trim() !== ''
  })
}

function mailServiceCardTitle(service) {
  const name = String(service?.name || '').trim()
  if (name) {
    return name
  }
  const label = mailServiceTypeLabel(service?.type)
  const domain = String(service?.domain || '').trim()
  return domain ? `${label} (${domain})` : label
}

function ensureMailServiceDefault() {
  const existingIds = new Set(mailServices.value.map(service => service.id))
  if (mailServiceDefault.value && existingIds.has(mailServiceDefault.value)) {
    return
  }
  mailServiceDefault.value = mailServices.value[0]?.id || ''
}

function addMailService(type) {
  mailServices.value = [...mailServices.value, createMailService(type)]
  ensureMailServiceDefault()
}

function removeMailService(id) {
  mailServices.value = mailServices.value.filter(service => service.id !== id)
  ensureMailServiceDefault()
}

function setDefaultMailService(id) {
  mailServiceDefault.value = id
}

function isRuntimeRequired(field) {
  return Boolean(field?.runtime_required) || runtimeRequiredKeys.has(field?.key)
}

function normalizeRuntimeFieldValue(field) {
  const value = field?.value ?? field?.default ?? ''
  if (isBooleanStringField(field?.key)) {
    return String(value).toLowerCase() === 'true' ? 'true' : 'false'
  }
  if (isWsModeField(field?.key)) {
    const mode = String(value || '').toLowerCase()
    return ['off', 'ctx_pool', 'passthrough'].includes(mode) ? mode : 'off'
  }
  return value
}

async function loadRuntimeConfig() {
  runtimeLoading.value = true
  try {
    const result = await api.getRuntimeConfig()
    runtimeFields.value = result.fields || []
    mailServices.value = Array.isArray(result.mail_services)
      ? result.mail_services.map(service => normalizeMailService(service))
      : []
    mailServiceDefault.value = String(result.mail_service_default || '')
    ensureMailServiceDefault()

    for (const key of Object.keys(runtimeForm)) {
      if (!runtimeFields.value.find(field => field.key === key)) {
        delete runtimeForm[key]
      }
    }
    for (const field of runtimeFields.value) {
      runtimeForm[field.key] = normalizeRuntimeFieldValue(field)
    }
    loadTeamRowsFromJson(false)
  } catch (e) {
    console.error('加载运行时配置失败:', e)
    setRuntimeMessage('加载运行时配置失败: ' + e.message, 'error')
  } finally {
    runtimeLoading.value = false
  }
}

async function saveRuntimeConfig() {
  runtimeSaving.value = true
  runtimeSaved.value = false
  try {
    if (selectedRuntimeCategory.value === 'teams') {
      syncTeamRowsToJson()
    }
    const payload = {}
    for (const field of runtimeFields.value) {
      const value = runtimeForm[field.key]
      payload[field.key] = value == null ? '' : String(value)
    }
    if (String(payload.CPA_URL || '').trim() || String(payload.CPA_KEY || '').trim()) {
      payload.SYNC_TARGET_CPA = 'true'
    }
    if (Object.prototype.hasOwnProperty.call(payload, 'SYNC_TARGET_SUB2API') && !String(payload.SYNC_TARGET_SUB2API || '').trim()) {
      payload.SYNC_TARGET_SUB2API = 'false'
    }
    const sanitizedServices = mailServices.value.map(service => sanitizeMailService(service))
    const sanitizedDefault = sanitizedServices.some(service => service.id === mailServiceDefault.value)
      ? mailServiceDefault.value
      : sanitizedServices[0]?.id || ''
    payload.mail_services = sanitizedServices
    payload.mail_service_default = sanitizedDefault
    const result = await api.saveRuntimeConfig(payload)
    if (result.api_key) {
      setApiKey(result.api_key)
    }
    setRuntimeMessage(result.message || '配置保存成功')
    runtimeSaved.value = true
    window.setTimeout(() => {
      runtimeSaved.value = false
    }, 3000)
    await loadRuntimeConfig()
    emit('refresh')
  } catch (e) {
    setRuntimeMessage(e.message, 'error')
  } finally {
    runtimeSaving.value = false
  }
}

async function loadSourceConfig() {
  sourceLoading.value = true
  try {
    const result = await api.getRuntimeConfigSource()
    sourcePath.value = result.path || '.env'
    sourceContent.value = result.content || ''
    sourceLoaded.value = true
  } catch (e) {
    console.error('加载源文件失败:', e)
    setSourceMessage('加载源文件失败: ' + e.message, 'error')
  } finally {
    sourceLoading.value = false
  }
}

async function saveSourceConfig() {
  sourceSaving.value = true
  try {
    const result = await api.saveRuntimeConfigSource({ content: sourceContent.value })
    if (result.api_key) {
      setApiKey(result.api_key)
    }
    setSourceMessage(result.message || '源文件保存成功')
    await Promise.all([loadSourceConfig(), loadRuntimeConfig()])
    emit('refresh')
  } catch (e) {
    setSourceMessage(e.message, 'error')
  } finally {
    sourceSaving.value = false
  }
}

watch(visualCategory, async (next) => {
  if (next === 'source' && !sourceLoaded.value) {
    await loadSourceConfig()
  }
})

watch(
  () => runtimeForm.TEAM_WORKSPACES_JSON,
  () => {
    if (!syncingTeamJsonFromRows) {
      loadTeamRowsFromJson(false)
    }
  },
)

watch(teamRows, () => {
  syncTeamRowsToJson()
}, { deep: true })

onMounted(async () => {
  await loadRuntimeConfig()
})
</script>
