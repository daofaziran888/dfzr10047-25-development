<template>
  <section class="null-release-panel" data-testid="null-release-panel">
    <div class="panel-heading">
      <h3>原值空位复核与发布核对</h3>
      <p>
        服务端按可空规则区分「合法业务空值」与「转换失败」；失败原值先进坏行隔离，
        修正回执批准后可回补。原发布申请重放不扩大冻结范围，迟到修正可单独办理。
      </p>
    </div>

    <div class="null-form-grid">
      <label>
        <span>关联批次</span>
        <input v-model="form.batch_id" data-testid="nr-batch-id" />
      </label>
      <label>
        <span>字段</span>
        <input v-model="form.field" data-testid="nr-field" />
      </label>
      <label>
        <span>目标类型</span>
        <input v-model="form.target_type" data-testid="nr-target-type" />
      </label>
      <label>
        <span>A 条目</span>
        <input v-model="form.aItem" aria-label="A 条目编号" data-testid="nr-a-item" />
      </label>
      <label>
        <span>A 行号</span>
        <input v-model.number="form.aRow" type="number" aria-label="A 原始行号" data-testid="nr-a-row" />
      </label>
      <label>
        <span>B 条目</span>
        <input v-model="form.bItem" aria-label="B 条目编号" data-testid="nr-b-item" />
      </label>
      <label>
        <span>B 行号</span>
        <input v-model.number="form.bRow" type="number" aria-label="B 原始行号" data-testid="nr-b-row" />
      </label>
      <label>
        <span>B 原字符串</span>
        <input v-model="form.bRaw" aria-label="B 原字符串" data-testid="nr-b-raw" />
      </label>
      <label>
        <span>B 修正值</span>
        <input v-model="form.bCorrected" aria-label="B 修正值" data-testid="nr-b-corrected" />
      </label>
      <label class="release-no-field">
        <span>发布申请编号</span>
        <input v-model="form.request_no" data-testid="nr-release-no" />
      </label>
    </div>

    <div class="null-actions">
      <button class="btn" type="button" data-testid="nr-register-rule" @click="registerRule">
        登记可空规则
      </button>
      <button class="btn" type="button" data-testid="nr-submit-a" @click="submitA">
        提交 A 空值判定
      </button>
      <button class="btn primary" type="button" data-testid="nr-authorize-a" @click="authorizeA">
        授权 A 的合法空值
      </button>
      <button class="btn primary" type="button" data-testid="nr-create-release" @click="createRelease">
        提交 A 放行申请
      </button>
      <button class="btn" type="button" data-testid="nr-submit-b" @click="submitB">
        对 B 的 xx 登记隔离
      </button>
      <button class="btn primary" type="button" data-testid="nr-approve-b" @click="approveB">
        复核员批准 B 修正为 7
      </button>
      <button class="btn" type="button" data-testid="nr-reinstate-b" @click="reinstateB">
        坏行隔离消费并回补 B
      </button>
      <button class="btn" type="button" data-testid="nr-confirm-release" @click="confirmRelease">
        确认原申请
      </button>
      <button class="btn" type="button" data-testid="nr-replay-release" @click="replayRelease">
        重试并重放一次
      </button>
      <button class="btn" type="button" data-testid="nr-publish-b-separate" @click="publishBSeparate">
        B 单独发布
      </button>
      <button class="btn ghost" type="button" data-testid="nr-run-story" @click="runStory">
        一键跑 A 空值 / B=xx→7 场景
      </button>
    </div>

    <p v-if="message" class="nr-message" data-testid="nr-message">{{ message }}</p>

    <div class="nr-result-grid">
      <article class="nr-card" data-testid="nr-a-card">
        <h4>A 合法空值</h4>
        <p data-testid="nr-a-status">{{ aRecord?.status ?? '未授权' }}</p>
        <pre data-testid="nr-a-json">{{ pretty(aRecord) }}</pre>
      </article>
      <article class="nr-card" data-testid="nr-b-card">
        <h4>B 原值 xx 与修正 7</h4>
        <p data-testid="nr-b-status">{{ bRecord?.status ?? '未提交' }}</p>
        <pre data-testid="nr-b-json">{{ pretty(bRecord) }}</pre>
      </article>
      <article class="nr-card release-card" data-testid="nr-release-card">
        <h4>确认页核对</h4>
        <p>本次收件：<strong data-testid="nr-received">{{ receivedText }}</strong></p>
        <p>还可单独办理：<strong data-testid="nr-separate">{{ separateText }}</strong></p>
        <p>
          重放次数 <span data-testid="nr-replay-count">{{ releaseRecord?.replay_count ?? 0 }}</span>
          ／实际生效 <span data-testid="nr-effective-count">{{ releaseRecord?.effective_delivery_count ?? 0 }}</span>
        </p>
        <pre data-testid="nr-release-json">{{ pretty(releaseRecord) }}</pre>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'

import { request } from '@/api/client'

type RecordMap = Record<string, any>

const form = reactive({
  batch_id: 'FI-UI-NULL-001',
  field: 'score',
  target_type: 'int64',
  aItem: 'A',
  aRow: 1,
  bItem: 'B',
  bRow: 2,
  bRaw: 'xx',
  bCorrected: '7',
  request_no: 'REL-UI-NULL-001',
})

const message = ref('')
const aRecord = ref<RecordMap | null>(null)
const bRecord = ref<RecordMap | null>(null)
const releaseRecord = ref<RecordMap | null>(null)

const receivedText = computed(() => {
  const items = releaseRecord.value?.received_items ?? releaseRecord.value?.本次收件
  return Array.isArray(items) ? items.map((item) => item.item ?? item).join(', ') || '—' : '—'
})
const separateText = computed(() => {
  const items = releaseRecord.value?.separately_releasable ?? releaseRecord.value?.separate_items
  return Array.isArray(items) ? items.join(', ') || '无' : '—'
})

function pretty(value: unknown): string {
  if (!value) return '{}'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

async function post(path: string, body: RecordMap): Promise<RecordMap> {
  message.value = ''
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json() as RecordMap
  if (!response.ok) {
    message.value = payload.message ?? '操作未成功'
    throw new Error(message.value)
  }
  // 原值判定为转换失败时 ok=false 但仍返回已签发/隔离的记录，页面需要继续展示并办理。
  message.value = payload.message ?? '操作完成'
  return payload.entry ?? payload.record ?? payload
}

async function registerRule() {
  await post('/api/value-cast/null-rules', {
    batch_id: form.batch_id,
    field: form.field,
    target_type: form.target_type,
    nullable: true,
  })
}

async function submitA() {
  aRecord.value = await post('/api/value-cast/original-values', {
    batch_id: form.batch_id,
    item: form.aItem,
    row_num: form.aRow,
    field: form.field,
    raw_value: '',
    target_type: form.target_type,
  })
}

async function authorizeA() {
  const no = aRecord.value?.复核编号 ?? `NV-${form.batch_id}-${form.aItem}-${form.aRow}`
  aRecord.value = await post(`/api/value-reviews/${encodeURIComponent(String(no))}/authorize`, {
    operator: 'ops-ui',
  })
}

async function submitB() {
  bRecord.value = await post('/api/value-cast/original-values', {
    batch_id: form.batch_id,
    item: form.bItem,
    row_num: form.bRow,
    field: form.field,
    raw_value: form.bRaw,
    target_type: form.target_type,
  })
}

async function approveB() {
  const no = bRecord.value?.复核编号 ?? `NV-${form.batch_id}-${form.bItem}-${form.bRow}`
  bRecord.value = await post(`/api/value-reviews/${encodeURIComponent(String(no))}/approve`, {
    reviewer: 'reviewer-ui',
    corrected_value: form.bCorrected,
    auto_reinstate: false,
  })
}

async function reinstateB() {
  const no = bRecord.value?.复核编号 ?? `NV-${form.batch_id}-${form.bItem}-${form.bRow}`
  bRecord.value = await post(`/api/value-reviews/${encodeURIComponent(String(no))}/reinstate`, {
    reviewer: 'reviewer-ui',
  })
}

async function createRelease() {
  releaseRecord.value = await post('/api/release-requests', {
    request_no: form.request_no,
    batch_id: form.batch_id,
    items: [form.aItem],
  })
}

async function confirmRelease() {
  releaseRecord.value = await post(
    `/api/release-requests/${encodeURIComponent(form.request_no)}/confirm`,
    {},
  )
}

async function replayRelease() {
  releaseRecord.value = await post(
    `/api/release-requests/${encodeURIComponent(form.request_no)}/replay`,
    {},
  )
}

async function publishBSeparate() {
  const record = await post(
    `/api/release-items/${encodeURIComponent(form.bItem)}/publish-separate`,
    { batch_id: form.batch_id },
  )
  releaseRecord.value = record
  message.value = record.投递结论 ?? message.value
}

async function runStory() {
  const response = await request('/api/value-release/story', {
    method: 'POST',
    body: JSON.stringify({
      batch_id: form.batch_id,
      field: form.field,
      target_type: form.target_type,
      a_item: form.aItem,
      a_row: form.aRow,
      b_item: form.bItem,
      b_row: form.bRow,
      b_raw: form.bRaw,
      corrected: form.bCorrected,
      request_no: form.request_no,
    }),
  })
  const payload = await response.json() as RecordMap
  aRecord.value = payload.a ?? null
  bRecord.value = payload.b ?? null
  releaseRecord.value = payload.release ?? null
  message.value = payload.message ?? '场景已执行'
}
</script>

<style scoped>
.null-release-panel {
  margin: 0 0 20px;
  padding: 18px;
  border: 1px solid #d8e2ea;
  border-radius: 14px;
  background: linear-gradient(180deg, #f8fbfd 0%, #ffffff 100%);
}

.panel-heading h3 {
  margin: 0 0 6px;
  font-size: 18px;
}

.panel-heading p {
  margin: 0 0 14px;
  color: #5b6b7a;
  line-height: 1.6;
}

.null-form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px 14px;
  margin-bottom: 12px;
}

.null-form-grid label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #425466;
}

.null-form-grid input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #c8d3dc;
  border-radius: 8px;
}

.null-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.nr-message {
  margin: 12px 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #eef7ff;
  color: #175a8a;
}

.nr-result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.nr-card {
  min-width: 0;
  padding: 12px;
  border: 1px solid #e1e8ef;
  border-radius: 10px;
  background: #fff;
}

.nr-card h4 {
  margin: 0 0 8px;
}

.nr-card p {
  margin: 6px 0;
}

.nr-card pre {
  max-height: 220px;
  overflow: auto;
  margin: 8px 0 0;
  padding: 8px;
  border-radius: 8px;
  background: #0f172a;
  color: #dbeafe;
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.release-card {
  grid-column: 1 / -1;
}
</style>
