<template>
  <section class="page" data-module="retry">
    <header class="page-head">
      <div>
        <h2>失败重试管理</h2>
        <p class="page-desc">
          失败批次按原始请求续做：退避倍数与降级端点随请求固定，中断后只补未完成尝试，
          同一策略版本下每个原始请求只保留一个成功结果。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记重试策略</button>
        <button class="btn" type="button" @click="exportRows">导出失败重试清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="btn"
        :class="activeTab === tab.key ? 'primary' : 'ghost'"
        type="button"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- 重试策略 -->
    <div v-if="activeTab === 'policy'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in policyColumns" :key="column">{{ column }}</th>
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in policies" :key="String(row.id)">
            <td v-for="column in policyColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>{{ row.status }}</td>
            <td class="row-actions">
              <button
                v-for="action in policyActions"
                :key="action"
                class="link"
                type="button"
                @click="runPolicyAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!policies.length">
            <td :colspan="policyColumns.length + 2" class="empty-state">暂无失败重试数据，可先登记重试策略</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 失败批次 -->
    <div v-if="activeTab === 'batch'">
      <form class="filter-bar" @submit.prevent="registerBatch">
        <label class="filter-item">
          <span>批次编号</span>
          <input v-model="batchForm.批次编号" placeholder="如 BATCH-9010" />
        </label>
        <label class="filter-item">
          <span>关联端点</span>
          <input v-model="batchForm.关联端点" placeholder="失败请求所属端点" />
        </label>
        <label class="filter-item">
          <span>策略编号</span>
          <input v-model="batchForm.策略编号" placeholder="如 RETR-0003" />
        </label>
        <label class="filter-item">
          <span>请求编号列表</span>
          <input v-model="batchForm.请求列表" placeholder="逗号分隔，如 REQ-1,REQ-2" />
        </label>
        <button class="btn primary" type="submit">登记失败批次</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in batchColumns" :key="column">{{ column }}</th>
            <th>续做批次</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in batches" :key="String(row.批次编号)">
            <td v-for="column in batchColumns" :key="column">
              {{ column === '全部成功' ? (row[column] ? '是' : '否') : row[column] ?? '—' }}
            </td>
            <td class="row-actions">
              <select v-model="batchChoice[String(row.批次编号)]" class="input">
                <option value="成功">结果=成功</option>
                <option value="失败">结果=失败</option>
                <option value="中断">模拟中断</option>
              </select>
              <button class="link" type="button" @click="resumeBatch(row)">续做未完成</button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td :colspan="batchColumns.length + 1" class="empty-state">暂无失败批次，登记后进入重试数据集</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 重试数据集 -->
    <div v-if="activeTab === 'dataset'">
      <form class="filter-bar" @submit.prevent="loadDataset">
        <label class="filter-item">
          <span>请求编号</span>
          <input v-model="datasetFilters.keyword" placeholder="按请求编号检索" />
        </label>
        <label class="filter-item">
          <span>批次编号</span>
          <input v-model="datasetFilters.batch" placeholder="按批次编号过滤" />
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="datasetFilters.status" class="input">
            <option value="">全部</option>
            <option v-for="status in datasetStatuses" :key="status" :value="status">{{ status }}</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetDatasetFilters">重置条件</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in datasetColumns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in dataset" :key="String(row.id)">
            <tr>
              <td v-for="column in datasetColumns" :key="column">{{ row[column] ?? '—' }}</td>
              <td class="row-actions">
                <template v-if="row.状态 === '待重试' || row.状态 === '重试中'">
                  <select v-model="resumeChoice[String(row.id)]" class="input">
                    <option value="成功">结果=成功</option>
                    <option value="失败">结果=失败</option>
                    <option value="中断">模拟中断</option>
                  </select>
                  <button class="link" type="button" @click="resumeRow(row)">续做</button>
                </template>
                <button
                  v-if="row.状态 === '已成功'"
                  class="link"
                  type="button"
                  @click="reportLateFailure(row)"
                >
                  补报失败
                </button>
                <template v-if="row.状态 === '冲突待裁决'">
                  <button class="link" type="button" @click="adjudicate(row, '重试')">裁决重试</button>
                  <button class="link" type="button" @click="adjudicate(row, '终止')">裁决终止</button>
                </template>
                <button class="link" type="button" @click="toggleDetail(row)">
                  {{ expandedId === row.id ? '收起序列' : '尝试序列' }}
                </button>
              </td>
            </tr>
            <tr v-if="expandedId === row.id" class="detail-row">
              <td :colspan="datasetColumns.length + 1">
                <div v-if="detail && detail.尝试序列 && detail.尝试序列.length">
                  <strong>执行序列（{{ detail.请求编号 }} · 策略版本 {{ detail.策略版本 }} · 队列状态 {{ detail.队列状态 ?? '—' }}）</strong>
                  <table class="data-table sub-table">
                    <thead>
                      <tr>
                        <th>尝试序号</th>
                        <th>使用端点</th>
                        <th>间隔秒</th>
                        <th>结果</th>
                        <th>记录时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="attempt in detail.尝试序列" :key="String(attempt.id)">
                        <td>{{ attempt.尝试序号 }}</td>
                        <td>{{ attempt.使用端点 }}</td>
                        <td>{{ attempt.间隔秒 }}</td>
                        <td>{{ attempt.结果 }}</td>
                        <td>{{ attempt.记录时间 }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <span v-else>该请求还没有尝试记录</span>
              </td>
            </tr>
          </template>
          <tr v-if="!dataset.length">
            <td :colspan="datasetColumns.length + 1" class="empty-state">重试数据集为空，可先在「失败批次」登记</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ datasetTotal }} 条重试数据集记录</span>
      </footer>
    </div>

    <!-- 待处理队列 -->
    <div v-if="activeTab === 'queue'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in queueColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in queue" :key="String(row.id)">
            <td v-for="column in queueColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!queue.length">
            <td :colspan="queueColumns.length" class="empty-state">失败批次待处理队列为空</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ queueTotal }} 条队列记录</span>
      </footer>
    </div>

    <footer class="page-foot">
      <span v-if="noticeMessage" class="ok-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/retry'
const tabs = [
  { key: 'dataset', label: '重试数据集' },
  { key: 'batch', label: '失败批次' },
  { key: 'queue', label: '待处理队列' },
  { key: 'policy', label: '重试策略' },
] as const
const policyColumns = ['策略编号', '关联端点', '最大重试', '退避倍数', '初始间隔秒', '最大间隔秒', '重试条件', '降级端点', '策略版本']
const policyActions = ['提交校验', '启用策略', '停用策略']
const batchColumns = ['批次编号', '请求数', '已成功', '未完成', '冲突待裁决', '已失败', '状态', '全部成功']
const datasetColumns = ['请求编号', '批次编号', '策略版本', '退避倍数', '降级端点', '已尝试次数', '下次间隔秒', '状态']
const queueColumns = ['批次编号', '请求编号', '策略版本', '状态', '已编入尝试数', '最近尝试结果', '更新时间']
const datasetStatuses = ['待重试', '重试中', '已成功', '已失败', '冲突待裁决']

const activeTab = ref<(typeof tabs)[number]['key']>('dataset')
const policies = ref<Row[]>([])
const batches = ref<Row[]>([])
const dataset = ref<Row[]>([])
const datasetTotal = ref(0)
const queue = ref<Row[]>([])
const queueTotal = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')

const batchForm = reactive({ 批次编号: '', 关联端点: '', 策略编号: '', 请求列表: '' })
const datasetFilters = reactive({ keyword: '', batch: '', status: '' })
const resumeChoice = reactive<Record<string, string>>({})
const batchChoice = reactive<Record<string, string>>({})
const expandedId = ref<number | null>(null)
const detail = ref<Row | null>(null)

const stats = computed(() => {
  const sum = (field: string) => batches.value.reduce((acc, row) => acc + Number(row[field] ?? 0), 0)
  const queued = queue.value.filter((row) => row.状态 === '待处理' || row.状态 === '重试中').length
  return [
    { label: '数据集请求', value: sum('请求数') },
    { label: '未完成请求', value: sum('未完成') },
    { label: '冲突待裁决', value: sum('冲突待裁决') },
    { label: '队列待处理', value: queued },
  ]
})

async function readJson(response: Response) {
  return response.json().catch(() => null)
}

async function postAction(path: string, values: Record<string, unknown>) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(path, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await readJson(response)
    if (!response.ok || !payload || payload.ok !== true) {
      throw new Error(payload?.message ?? payload?.detail ?? `接口返回 ${response.status}，重试动作未生效`)
    }
    noticeMessage.value = String(payload.message ?? '动作已生效')
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试动作未生效，请稍后重试'
  }
}

function runPolicyAction(action: string, row: Row) {
  void postAction(`${ENDPOINT}/${row.id}/actions`, { action })
}

function registerBatch() {
  void postAction(`${ENDPOINT}/batches`, { ...batchForm })
}

function resumeRow(row: Row) {
  const choice = resumeChoice[String(row.id)] ?? '成功'
  const values = choice === '中断' ? { 中断: true } : { 结果: choice }
  void postAction(`${ENDPOINT}/dataset/${row.id}/resume`, values)
}

function resumeBatch(row: Row) {
  const code = String(row.批次编号)
  const choice = batchChoice[code] ?? '成功'
  const values = choice === '中断' ? { 中断: true } : { 结果: choice }
  void postAction(`${ENDPOINT}/batches/${code}/resume`, values)
}

function reportLateFailure(row: Row) {
  void postAction(`${ENDPOINT}/dataset/${row.id}/report`, { 结果: '失败' })
}

function adjudicate(row: Row, decision: string) {
  void postAction(`${ENDPOINT}/dataset/${row.id}/adjudicate`, { 裁决: decision })
}

async function toggleDetail(row: Row) {
  errorMessage.value = ''
  if (expandedId.value === row.id) {
    expandedId.value = null
    detail.value = null
    return
  }
  try {
    const response = await request(`${ENDPOINT}/dataset/${row.id}`)
    const payload = await readJson(response)
    if (!response.ok || !payload) {
      throw new Error(payload?.detail ?? '重试详情读取失败')
    }
    detail.value = payload
    expandedId.value = row.id
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试详情读取失败'
  }
}

function resetDatasetFilters() {
  datasetFilters.keyword = ''
  datasetFilters.batch = ''
  datasetFilters.status = ''
  void loadDataset()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = ''
  noticeMessage.value = '重试策略登记入口尚未接入审批流'
}

async function loadPolicies() {
  const response = await request(`${ENDPOINT}?size=200`)
  const payload = await readJson(response)
  if (!response.ok || !payload) throw new Error('重试策略列表读取失败')
  policies.value = payload.items ?? []
}

async function loadBatches() {
  const response = await request(`${ENDPOINT}/batches`)
  const payload = await readJson(response)
  if (!response.ok || !payload) throw new Error('失败批次汇总读取失败')
  batches.value = payload.items ?? []
}

async function loadDataset() {
  const query = new URLSearchParams()
  if (datasetFilters.keyword) query.set('keyword', datasetFilters.keyword)
  if (datasetFilters.batch) query.set('batch', datasetFilters.batch)
  if (datasetFilters.status) query.set('status', datasetFilters.status)
  query.set('size', '200')
  const response = await request(`${ENDPOINT}/dataset?${query.toString()}`)
  const payload = await readJson(response)
  if (!response.ok || !payload) throw new Error('重试数据集读取失败')
  dataset.value = payload.items ?? []
  datasetTotal.value = payload.total ?? dataset.value.length
}

async function loadQueue() {
  const response = await request(`${ENDPOINT}/queue?size=200`)
  const payload = await readJson(response)
  if (!response.ok || !payload) throw new Error('待处理队列读取失败')
  queue.value = payload.items ?? []
  queueTotal.value = payload.total ?? queue.value.length
}

async function reloadAll() {
  try {
    await Promise.all([loadPolicies(), loadBatches(), loadDataset(), loadQueue()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失败重试数据读取失败'
  }
}

onMounted(reloadAll)
</script>
