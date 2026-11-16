<template>
  <section class="page" data-module="log">
    <header class="page-head">
      <div>
        <h2>采集日志管理</h2>
        <p class="page-desc">维护采集日志记录的输入、执行约束与结果记录，为采集日志提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记日志记录</button>
        <button class="btn" type="button" @click="exportRows">导出采集日志清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="String(row.id)">
          <tr>
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
              <button class="link" type="button" @click="toggleAttempts(row)">
                {{ attemptRowId === row.id ? '收起序列' : '尝试序列' }}
              </button>
            </td>
          </tr>
          <tr v-if="attemptRowId === row.id" class="detail-row">
            <td :colspan="columns.length + 1">
              <div v-if="attemptPanel && attemptPanel.尝试序列.length">
                <strong>
                  完整尝试序列（{{ attemptPanel.记录编号 }} · {{ attemptPanel.关联端点 }} ·
                  {{ attemptPanel.请求数 }} 个原始请求）
                </strong>
                <table class="data-table sub-table">
                  <thead>
                    <tr>
                      <th>请求编号</th>
                      <th>尝试序号</th>
                      <th>使用端点</th>
                      <th>间隔秒</th>
                      <th>结果</th>
                      <th>记录时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="attempt in attemptPanel.尝试序列" :key="String(attempt.id)">
                      <td>{{ attempt.请求编号 }}</td>
                      <td>{{ attempt.尝试序号 }}</td>
                      <td>{{ attempt.使用端点 }}</td>
                      <td>{{ attempt.间隔秒 }}</td>
                      <td>{{ attempt.结果 }}</td>
                      <td>{{ attempt.记录时间 }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <span v-else>该日志关联端点下没有重试尝试记录</span>
            </td>
          </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集日志数据，可先登记日志记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集日志记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/log'
const columns = ["记录编号", "关联端点", "请求时间", "响应状态", "耗时毫秒", "返回行数", "错误信息", "重试次数"]
const actions = ["归档记录", "清理日志", "导出日志"]
const statuses = ["草稿", "记录中", "已归档", "已清理"]
const stats = [{"label": "日志记录总数", "value": 0}, {"label": "已归档记录", "value": 0}, {"label": "记录中日志", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const attemptRowId = ref<string | number | null>(null)
const attemptPanel = ref<Record<string, any> | null>(null)

async function toggleAttempts(row: Row) {
  errorMessage.value = ''
  if (attemptRowId.value === row.id) {
    attemptRowId.value = null
    attemptPanel.value = null
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/attempts`)
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload) {
      throw new Error(payload?.detail ?? '尝试序列读取失败')
    }
    attemptPanel.value = payload
    attemptRowId.value = row.id ?? null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '尝试序列读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '日志记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload || payload.ok !== true) {
      throw new Error(payload?.message ?? payload?.detail ?? '采集日志动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集日志操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('日志记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集日志列表读取失败'
  }
}

onMounted(reload)
</script>
