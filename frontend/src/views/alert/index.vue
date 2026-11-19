<template>
  <section class="page" data-module="alert">
    <header class="page-head">
      <div>
        <h2>智能告警管理</h2>
        <p class="page-desc">维护告警规则，围绕规则编号、告警名称、监控指标、阈值条件做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记告警规则</button>
        <button class="btn" type="button" @click="exportRows">导出智能告警清单</button>
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
        <tr v-for="row in rows" :key="String(row.id)">
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无智能告警数据，可先登记告警规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条智能告警记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <h3 class="calib-title">分层误报校准表（告警判定依据）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>模型编号</th>
          <th>校准批次</th>
          <th>可试用阈值范围</th>
          <th>覆盖分层 / 流量</th>
          <th>各层有效负例权重</th>
          <th>不可试用流量清单</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in calibrationItems" :key="item['校准批次编号']">
          <td>{{ item['模型编号'] }}</td>
          <td>{{ item['校准批次编号'] }}</td>
          <td>
            <span v-if="item['可试用']">
              [{{ item['可试用阈值范围']?.min }}, {{ item['可试用阈值范围']?.max }}]
            </span>
            <span v-else>不可试用</span>
          </td>
          <td>{{ item['覆盖分层'].join('、') || '—' }}（{{ formatPercent(item['可覆盖流量占比']) }}）</td>
          <td>
            <span v-for="layer in item['判定依据']['分层有效负例权重']" :key="layer.layer" class="layer-chip">
              {{ layer.layer }}：{{ layer.negative_missing ? '缺负例' : layer.effective_negative_weight }}
            </span>
          </td>
          <td>
            <span v-if="!item['不可试用流量清单'].length">—</span>
            <span v-for="blocked in item['不可试用流量清单']" :key="blocked.layer" class="blocked-chip">
              {{ blocked.layer }}（待补 {{ blocked.required_extra_negative_weight }}）
            </span>
          </td>
        </tr>
        <tr v-if="!calibrationItems.length">
          <td colspan="6" class="empty-state">暂无已确认的分层误报校准批次</td>
        </tr>
      </tbody>
    </table>

    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
        <div class="modal-card">
          <header class="modal-head">
            <h3>登记告警规则</h3>
            <button class="btn ghost" type="button" @click="showCreateModal = false">&times;</button>
          </header>
          <form class="modal-body" @submit.prevent="submitCreate">
            <label v-for="field in columns.slice(0, 3)" :key="field" class="modal-field">
              <span>{{ field }}</span>
              <input v-model="createFields[field]" :placeholder="`请输入${field}`" required />
            </label>
            <footer class="modal-foot">
              <button class="btn" type="button" @click="showCreateModal = false">取消</button>
              <button class="btn primary" type="submit">提交登记</button>
            </footer>
          </form>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface LayerBasis {
  layer: string
  effective_negative_weight: number
  negative_missing: boolean
}

interface BlockedLayer {
  layer: string
  required_extra_negative_weight: number
}

interface CalibrationConsumption {
  模型编号: string
  校准批次编号: string
  可试用: boolean
  可试用阈值范围: { min: number; max: number } | null
  覆盖分层: string[]
  可覆盖流量占比: number
  不可试用流量清单: BlockedLayer[]
  判定依据: { 分层有效负例权重: LayerBasis[] }
}

const ENDPOINT = '/api/alert'
const columns = ["规则编号", "告警名称", "监控指标", "阈值条件", "告警级别", "通知方式", "静默时段", "规则状态"]
const actions = ["保存配置", "启用规则", "停用规则"]
const statuses = ["草稿", "已配置", "生效中", "已停用"]
const stats = [{"label": "生效中规则", "value": 0}, {"label": "草稿规则", "value": 0}, {"label": "停用规则", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreateModal = ref(false)
const createFields = ref<Record<string, string>>({})
const calibrationItems = ref<CalibrationConsumption[]>([])

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(2)}%`
}

async function loadCalibrationConsumption(): Promise<void> {
  try {
    const response = await request(`${ENDPOINT}/calibration-consumption`)
    if (!response.ok) return
    const payload = await response.json()
    calibrationItems.value = payload.items ?? []
  } catch {
    // 校准表读取失败不影响告警规则主表展示
  }
}

function updateStats() {
  const data = rows.value
  stats.length > 0 && (stats[0].value = data.filter((r: Row) => r.status === statuses[1] || r.pending).length)
  stats.length > 1 && (stats[1].value = data.filter((r: Row) => r.status && !r.pending && !r.abnormal).length)
  stats.length > 2 && (stats[2].value = data.filter((r: Row) => r.abnormal).length)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = ''
  createFields.value = {}
  showCreateModal.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createFields.value }),
    })
    if (!response.ok) {
      const data = await response.json()
      throw new Error(data.message || '登记失败')
    }
    showCreateModal.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('智能告警动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '智能告警操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('告警规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    updateStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '智能告警列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadCalibrationConsumption()
})
</script>

<style scoped>
.calib-title { margin: 20px 0 8px; font-size: 16px; }
.layer-chip { display: inline-block; margin: 0 6px 4px 0; padding: 2px 8px; background: #f0f4ff; border-radius: 10px; font-size: 12px; }
.blocked-chip { display: inline-block; margin: 0 6px 4px 0; padding: 2px 8px; background: #fdecea; color: #b42318; border-radius: 10px; font-size: 12px; }
</style>
