<template>
  <section class="page" data-module="calibration">
    <header class="page-head">
      <div>
        <h2>分层误报校准</h2>
        <p class="page-desc">
          从模型编号关联的评估批次接收已复核样本，按流量分层计算各阈值误报率与有效样本量，
          输出可试用阈值范围、不可试用流量清单与告警判定依据。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新数据</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">评估批次</span>
        <strong class="stat-value">{{ evalBatches.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已确认校准批次</span>
        <strong class="stat-value">{{ batches.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">最新可试用阈值</span>
        <strong class="stat-value">{{ trialRangeText }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可覆盖流量</span>
        <strong class="stat-value">{{ coveredShareText }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <h3 class="section-title">评估批次（已复核样本）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>评估批次编号</th>
          <th>模型编号</th>
          <th>批次名称</th>
          <th>评估时间</th>
          <th>流量分层</th>
          <th>样本数</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in evalBatches" :key="String(row.id)">
          <td>{{ row['评估批次编号'] }}</td>
          <td>{{ row['模型编号'] }}</td>
          <td>{{ row['批次名称'] }}</td>
          <td>{{ row['评估时间'] }}</td>
          <td>
            <span v-for="layer in row['流量分层']" :key="layer.layer" class="layer-chip">
              {{ layer.layer }}（{{ formatPercent(layer.traffic_share) }}）
            </span>
          </td>
          <td>{{ row.samples.length }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="selectBatch(row)">选择并校准</button>
          </td>
        </tr>
        <tr v-if="!evalBatches.length">
          <td colspan="7" class="empty-state">暂无已复核评估批次</td>
        </tr>
      </tbody>
    </table>

    <div v-if="selectedBatch" class="panel">
      <h3 class="section-title">提交校准批次：{{ selectedBatch['评估批次编号'] }}</h3>
      <form class="filter-bar" @submit.prevent="submitCalibration">
        <label class="filter-item">
          <span>误报率上限</span>
          <input v-model="submitForm.max_fpr" type="number" min="0" max="1" step="0.01" required />
        </label>
        <label class="filter-item">
          <span>总体最低有效负例权重</span>
          <input v-model="submitForm.min_negative_weight" type="number" min="1" step="1" required />
        </label>
        <label class="filter-item">
          <span>幂等键（可选）</span>
          <input v-model="submitForm.request_id" placeholder="并发提交去重键" />
        </label>
        <button class="btn primary" type="submit">提交校准</button>
      </form>
    </div>

    <template v-if="latestBatch">
      <h3 class="section-title">
        最新校准结果：{{ latestBatch['校准批次编号'] }}
        <span class="version-tag">版本 {{ latestBatch['版本号'] }}</span>
      </h3>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">总体有效负例权重</span>
          <strong class="stat-value">{{ latestBatch.result.overall.negative_weight }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">总体有效样本权重</span>
          <strong class="stat-value">{{ latestBatch.result.overall.effective_sample_weight }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">复核冲突样本</span>
          <strong class="stat-value">{{ latestBatch.result.conflicts.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">不可试用分层</span>
          <strong class="stat-value">{{ latestBatch.trial.uncovered_layers.length }}</strong>
        </article>
      </div>

      <h4 class="sub-title">分层有效样本量与试用覆盖</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>流量分层</th>
            <th>流量占比</th>
            <th>有效样本数</th>
            <th>有效样本权重</th>
            <th>有效负例权重（分母）</th>
            <th>冲突样本</th>
            <th>缺负例</th>
            <th>分层最低负例权重</th>
            <th>试用状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="layer in latestBatch.result.layers" :key="layer.scope">
            <td>{{ layer.scope }}</td>
            <td>{{ formatPercent(layer.traffic_share) }}</td>
            <td>{{ layer.effective_sample_count }}</td>
            <td>{{ layer.effective_sample_weight }}</td>
            <td>{{ layer.negative_missing ? '不可计算' : layer.negative_weight }}</td>
            <td>{{ layer.conflict_count }}（{{ layer.conflict_sample_ids.join('、') || '—' }}）</td>
            <td>{{ layer.negative_missing ? '是，禁止按零误报处理' : '否' }}</td>
            <td>{{ layer.min_negative_weight }}</td>
            <td>{{ coveredLayerNames.includes(layer.scope) ? '可覆盖' : '暂未覆盖' }}</td>
          </tr>
        </tbody>
      </table>

      <h4 class="sub-title">总体阈值明细（误报率 = 误报负例权重 / 有效负例权重）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>阈值</th>
            <th>有效负例权重</th>
            <th>误报负例权重</th>
            <th>误报率</th>
            <th>是否可试用</th>
            <th>判定原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="point in latestBatch.result.overall.thresholds" :key="String(point.threshold)">
            <td>{{ point.threshold }}</td>
            <td>{{ point.effective_negative_weight }}</td>
            <td>{{ point.false_positive_weight }}</td>
            <td>{{ point.false_alarm_rate === null ? '不可计算' : formatPercent(point.false_alarm_rate) }}</td>
            <td>
              <span :class="point.eligible ? 'tag ok' : 'tag no'">
                {{ point.eligible ? '可试用' : '不可试用' }}
              </span>
            </td>
            <td>{{ point.reason }}</td>
          </tr>
        </tbody>
      </table>

      <div class="two-col">
        <div>
          <h4 class="sub-title">复核冲突样本（权重只计一次）</h4>
          <table class="data-table">
            <thead>
              <tr><th>样本编号</th><th>分层</th><th>权重</th><th>历次复核标签</th><th>最终标签</th></tr>
            </thead>
            <tbody>
              <tr v-for="conflict in latestBatch.result.conflicts" :key="conflict.sample_id">
                <td>{{ conflict.sample_id }}</td>
                <td>{{ conflict.layer }}</td>
                <td>{{ conflict.weight }}</td>
                <td>{{ conflict.review_labels.join(' → ') }}</td>
                <td>{{ conflict.final_label }}</td>
              </tr>
              <tr v-if="!latestBatch.result.conflicts.length">
                <td colspan="5" class="empty-state">无复核冲突</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4 class="sub-title">不可试用流量清单（需继续复核）</h4>
          <table class="data-table">
            <thead>
              <tr><th>分层</th><th>流量占比</th><th>有效负例权重</th><th>待补负例权重</th><th>原因</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in latestBatch.trial.uncovered_layers" :key="item.layer">
                <td>{{ item.layer }}</td>
                <td>{{ formatPercent(item.traffic_share) }}</td>
                <td>{{ item.effective_negative_weight }}</td>
                <td>{{ item.required_extra_negative_weight }}</td>
                <td>{{ item.reason }}</td>
              </tr>
              <tr v-if="!latestBatch.trial.uncovered_layers.length">
                <td colspan="5" class="empty-state">全部分层均可覆盖</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <h4 class="sub-title">复核更正标签并重新计算</h4>
      <form class="filter-bar" @submit.prevent="correctLabel">
        <label class="filter-item">
          <span>样本编号</span>
          <input v-model="correctForm.sample_id" placeholder="如 A-N001" required />
        </label>
        <label class="filter-item">
          <span>更正标签</span>
          <select v-model="correctForm.new_label" required>
            <option value="正例">正例</option>
            <option value="负例">负例</option>
          </select>
        </label>
        <label class="filter-item">
          <span>更正原因</span>
          <input v-model="correctForm.reason" placeholder="复核说明" />
        </label>
        <button class="btn primary" type="submit">更正并重算</button>
      </form>

      <h4 class="sub-title">校准批次历史版本</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>校准批次编号</th>
            <th>版本号</th>
            <th>可试用阈值范围</th>
            <th>覆盖分层</th>
            <th>创建时间</th>
            <th>更正记录</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in batches" :key="String(row.id)">
            <td>{{ row['校准批次编号'] }}</td>
            <td>{{ row['版本号'] }}</td>
            <td>{{ row.trial.trialable ? `[${row.trial.threshold_min}, ${row.trial.threshold_max}]` : '不可试用' }}</td>
            <td>{{ row.trial.covered_layers.join('、') || '—' }}</td>
            <td>{{ row['创建时间'] }}</td>
            <td>{{ row['更正记录'] ? `${row['更正记录'].sample_id}：${row['更正记录'].old_label}→${row['更正记录'].new_label}` : '—' }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface TrafficLayer {
  layer: string
  traffic_share: number
  min_negative_weight: number
}

interface EvalBatch {
  id: number
  评估批次编号: string
  模型编号: string
  批次名称: string
  评估时间: string
  流量分层: TrafficLayer[]
  samples: unknown[]
}

interface ThresholdPoint {
  threshold: number
  effective_negative_weight: number
  false_positive_weight: number
  false_alarm_rate: number | null
  eligible: boolean
  reason: string
}

interface LayerMetric {
  scope: string
  traffic_share: number | null
  effective_sample_count: number
  effective_sample_weight: number
  negative_weight: number
  negative_missing: boolean
  conflict_count: number
  conflict_sample_ids: string[]
  min_negative_weight: number
  thresholds: ThresholdPoint[]
}

interface ConflictItem {
  sample_id: string
  layer: string
  weight: number
  review_labels: string[]
  final_label: string
}

interface UncoveredItem {
  layer: string
  traffic_share: number | null
  effective_negative_weight: number
  required_extra_negative_weight: number
  reason: string
}

interface Trial {
  trialable: boolean
  threshold_min: number | null
  threshold_max: number | null
  covered_layers: string[]
  covered_traffic_share: number
  uncovered_layers: UncoveredItem[]
}

interface CalibrationBatch {
  id: number
  校准批次编号: string
  版本号: number
  创建时间: string
  trial: Trial
  result: {
    overall: { negative_weight: number; effective_sample_weight: number; thresholds: ThresholdPoint[] }
    layers: LayerMetric[]
    conflicts: ConflictItem[]
  }
  更正记录?: { sample_id: string; old_label: string; new_label: string }
}

const evalBatches = ref<EvalBatch[]>([])
const batches = ref<CalibrationBatch[]>([])
const selectedBatch = ref<EvalBatch | null>(null)
const errorMessage = ref('')
const submitForm = ref({ max_fpr: 0.05, min_negative_weight: 100, request_id: '' })
const correctForm = ref({ sample_id: '', new_label: '正例', reason: '' })

const latestBatch = computed<CalibrationBatch | null>(() => {
  if (!batches.value.length) return null
  return [...batches.value].sort((a, b) => b.版本号 - a.版本号)[0]
})

const coveredLayerNames = computed<string[]>(() => latestBatch.value?.trial.covered_layers ?? [])

const trialRangeText = computed(() => {
  const trial = latestBatch.value?.trial
  if (!trial || !trial.trialable) return '不可试用'
  return `[${trial.threshold_min}, ${trial.threshold_max}]`
})

const coveredShareText = computed(() => {
  const trial = latestBatch.value?.trial
  if (!trial) return '—'
  return formatPercent(trial.covered_traffic_share)
})

function formatPercent(value: number | null): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(2)}%`
}

function selectBatch(row: EvalBatch): void {
  selectedBatch.value = row
  errorMessage.value = ''
}

async function reload(): Promise<void> {
  errorMessage.value = ''
  try {
    const [evalResp, batchResp] = await Promise.all([
      request('/api/calibration/eval-batches'),
      request('/api/calibration/batches?size=200'),
    ])
    if (!evalResp.ok || !batchResp.ok) throw new Error('校准数据读取失败')
    const evalPayload = await evalResp.json()
    const batchPayload = await batchResp.json()
    evalBatches.value = evalPayload.items ?? []
    batches.value = batchPayload.items ?? []
    if (!selectedBatch.value && evalBatches.value.length) {
      selectedBatch.value = evalBatches.value[0]
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '校准数据读取失败'
  }
}

async function submitCalibration(): Promise<void> {
  if (!selectedBatch.value) return
  errorMessage.value = ''
  try {
    const response = await request(
      `/api/calibration/eval-batches/${selectedBatch.value.id}/submit`,
      {
        method: 'POST',
        body: JSON.stringify({
          max_fpr: Number(submitForm.value.max_fpr),
          min_negative_weight: Number(submitForm.value.min_negative_weight),
          request_id: submitForm.value.request_id || null,
        }),
      },
    )
    const data = await response.json()
    if (!response.ok || !data.ok) throw new Error(data.message || '校准提交失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '校准提交失败'
  }
}

async function correctLabel(): Promise<void> {
  if (!selectedBatch.value) return
  errorMessage.value = ''
  try {
    const response = await request(
      `/api/calibration/eval-batches/${selectedBatch.value.id}/samples/${encodeURIComponent(correctForm.value.sample_id)}/correct`,
      {
        method: 'POST',
        body: JSON.stringify({
          new_label: correctForm.value.new_label,
          reason: correctForm.value.reason,
        }),
      },
    )
    const data = await response.json()
    if (!response.ok || !data.ok) throw new Error(data.message || '标签更正失败')
    correctForm.value = { sample_id: '', new_label: '正例', reason: '' }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签更正失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title { margin: 20px 0 8px; font-size: 16px; }
.sub-title { margin: 16px 0 8px; font-size: 14px; color: var(--muted, #666); }
.panel { background: #fff; border: 1px solid var(--border, #e5e5e5); border-radius: 8px; padding: 12px; margin-top: 12px; }
.layer-chip { display: inline-block; margin-right: 6px; padding: 2px 8px; background: #f0f4ff; border-radius: 10px; font-size: 12px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.tag.ok { background: #e6f7ec; color: #1a7f37; }
.tag.no { background: #fdecea; color: #b42318; }
.version-tag { margin-left: 8px; font-size: 12px; color: #fff; background: #3559e0; border-radius: 10px; padding: 2px 8px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
</style>
