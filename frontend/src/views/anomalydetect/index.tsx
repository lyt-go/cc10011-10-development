import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/anomalydetect'
const columns = [
  "模型编号",
  "监控指标",
  "检测算法",
  "训练窗口天",
  "灵敏度参数",
  "季节周期",
  "节假日因子",
  "模型更新时间",
  "模型版本",
  "部署版本"
]
const actions = [
  "提交训练",
  "部署模型",
  "停用模型"
]
const statuses = [
  "草稿",
  "模型训练中",
  "已部署",
  "已停用"
]
const stats = [
  {
    "label": "检测模型总数",
    "value": 0
  },
  {
    "label": "已部署模型",
    "value": 0
  },
  {
    "label": "训练中模型",
    "value": 0
  }
]
const snapshotColumns = [
  "部署版本",
  "模型编号",
  "模型版本",
  "监控指标",
  "检测算法",
  "训练窗口开始",
  "训练窗口结束",
  "快照状态"
]
const datasetColumns = [
  "业务批次",
  "数据集编号",
  "部署版本",
  "模型版本",
  "训练窗口开始",
  "训练窗口结束",
  "数据集状态",
  "事件总数",
  "异常事件数"
]

type Row = Record<string, string | number | null>
type Snapshot = Record<string, string | number | null>
type Dataset = Record<string, string | number | null>

export default function AnomalydetectView() {
  const [rows, setRows] = useState<Row[]>([])
  const [statRows, setStatRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => ["模型编号", "监控指标", "检测算法"], [])

  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [events, setEvents] = useState<Snapshot[]>([])
  const [activeBatch, setActiveBatch] = useState('')
  const [batchInput, setBatchInput] = useState('')
  const [snapshotInput, setSnapshotInput] = useState('')
  const [notice, setNotice] = useState('')

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('检测模型列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      // 统计卡片看全量，不受当前分页与筛选影响。
      const allResponse = await request(`${ENDPOINT}?size=200`)
      if (allResponse.ok) {
        const allPayload = await allResponse.json()
        setStatRows(allPayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '异常检测列表读取失败')
    }
  }, [filters])

  const statCards = useMemo(() => {
    const deployed = statRows.filter((row) => row.status === '已部署').length
    const training = statRows.filter((row) => row.status === '模型训练中').length
    return stats.map((item, index) => ({
      ...item,
      value: index === 0 ? Math.max(total, statRows.length) : index === 1 ? deployed : training,
    }))
  }, [statRows, total])

  const reloadTraces = useCallback(async () => {
    try {
      const [snapshotResponse, datasetResponse] = await Promise.all([
        request(`${ENDPOINT}/snapshots`),
        request(`${ENDPOINT}/datasets?size=50`),
      ])
      if (snapshotResponse.ok) {
        const payload = await snapshotResponse.json()
        setSnapshots(payload.items ?? [])
      }
      if (datasetResponse.ok) {
        const payload = await datasetResponse.json()
        setDatasets(payload.items ?? [])
      }
    } catch {
      // 追溯区读取失败不覆盖主列表的错误提示
    }
  }, [])

  const reloadEvents = useCallback(async (batch: string) => {
    if (!batch) {
      setEvents([])
      return
    }
    try {
      const response = await request(`${ENDPOINT}/events?batch=${encodeURIComponent(batch)}&only_anomaly=true`)
      if (response.ok) {
        const payload = await response.json()
        setEvents(payload.items ?? [])
      }
    } catch {
      // 同上：事件明细单独失败时保持页面其他内容可用
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadTraces()
  }, [reload, reloadTraces])

  useEffect(() => {
    void reloadEvents(activeBatch)
  }, [activeBatch, reloadEvents])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('异常检测动作未生效，请稍后重试')
      }
      const payload = await response.json()
      if (payload.ok === false) {
        setErrorMessage(String(payload.message ?? '异常检测动作未生效'))
      } else {
        setNotice(String(payload.message ?? ''))
      }
      await reload()
      await reloadTraces()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '异常检测操作失败')
    }
  }

  async function generateDataset() {
    setErrorMessage('')
    setNotice('')
    const snapshotId = Number(snapshotInput)
    if (!batchInput.trim() || !Number.isFinite(snapshotId) || snapshotId <= 0) {
      setErrorMessage('请填写业务批次，并选择或输入有效的部署快照编号')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/datasets`, {
        method: 'POST',
        body: JSON.stringify({ values: { 业务批次: batchInput.trim(), 部署快照编号: snapshotId } }),
      })
      if (!response.ok) {
        throw new Error('异常事件数据集生成请求未送达')
      }
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(String(payload.message ?? '异常事件数据集未生成'))
      } else {
        setNotice(String(payload.message ?? ''))
        setActiveBatch(batchInput.trim())
      }
      await reloadTraces()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '异常事件数据集生成失败')
    }
  }

  return (
    <section className="page" data-module="anomalydetect">
      <header className="page-head">
        <div>
          <h2>异常检测管理</h2>
          <p className="page-desc">
            读取监控指标时序与检测模型参数训练模型，部署时生成带版本指纹的快照，
            异常结果落成按业务批次可追溯的事件数据集。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('检测模型登记入口尚未接入审批流')}>
            登记检测模型
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出异常检测清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] || '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无异常检测数据，可先登记检测模型</td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="panel">
        <h3 className="panel-title">部署快照</h3>
        <p className="panel-desc">
          每个部署版本由模型版本、训练窗口、参数与环境指纹共同决定；同一次训练重复部署得到同一指纹。
        </p>
        <table className="data-table">
          <thead>
            <tr>
              {snapshotColumns.map((column) => <th key={column}>{column}</th>)}
              <th>用于生成</th>
            </tr>
          </thead>
          <tbody>
            {snapshots.map((snapshot) => (
              <tr key={String(snapshot.id)}>
                {snapshotColumns.map((column) => <td key={column}>{snapshot[column] || '—'}</td>)}
                <td className="row-actions">
                  <button
                    className="link"
                    type="button"
                    disabled={snapshot["快照状态"] !== '生效中'}
                    onClick={() => setSnapshotInput(String(snapshot.id))}
                  >
                    选为部署快照
                  </button>
                </td>
              </tr>
            ))}
            {snapshots.length === 0 && (
              <tr><td className="empty-state" colSpan={snapshotColumns.length + 1}>还没有部署快照，部署检测模型后会生成</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3 className="panel-title">异常检测事件数据集</h3>
        <p className="panel-desc">
          一个业务批次只落一份完整数据集，状态沿「待训练 → 模型训练中 → 快照已生成 → 已完成」向前推进。
        </p>
        <div className="filter-bar">
          <label className="filter-item">
            <span>业务批次</span>
            <input value={batchInput} placeholder="例如 B-20260902" onChange={(event) => setBatchInput(event.target.value)} />
          </label>
          <label className="filter-item">
            <span>部署快照编号</span>
            <input value={snapshotInput} placeholder="从上方快照选择" onChange={(event) => setSnapshotInput(event.target.value)} />
          </label>
          <button className="btn primary" type="button" onClick={() => void generateDataset()}>生成事件数据集</button>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              {datasetColumns.map((column) => <th key={column}>{column}</th>)}
              <th>查看事件</th>
            </tr>
          </thead>
          <tbody>
            {datasets.map((dataset) => (
              <tr key={String(dataset.id)}>
                {datasetColumns.map((column) => <td key={column}>{dataset[column] ?? '—'}</td>)}
                <td className="row-actions">
                  <button className="link" type="button" onClick={() => setActiveBatch(String(dataset["业务批次"] ?? ''))}>
                    异常事件
                  </button>
                </td>
              </tr>
            ))}
            {datasets.length === 0 && (
              <tr><td className="empty-state" colSpan={datasetColumns.length + 1}>还没有事件数据集，先部署模型再按批次生成</td></tr>
            )}
          </tbody>
        </table>
        {activeBatch && (
          <div className="panel">
            <h3 className="panel-title">批次 {activeBatch} 的异常事件</h3>
            <p className="panel-desc">每条事件都绑定生成它的部署版本与训练窗口，告警历史读取的是同一份记录。</p>
            <table className="data-table">
              <thead>
                <tr>
                  <th>事件编号</th><th>事件时间</th><th>指标值</th><th>基线值</th>
                  <th>严重度</th><th>模型版本</th><th>部署版本</th>
                  <th>训练窗口开始</th><th>训练窗口结束</th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <tr key={String(event.id)}>
                    <td>{event["事件编号"]}</td>
                    <td>{event["事件时间"]}</td>
                    <td>{event["指标值"]}</td>
                    <td>{event["基线值"]}</td>
                    <td>{event["严重度"]}</td>
                    <td>{event["模型版本"]}</td>
                    <td>{event["部署版本"]}</td>
                    <td>{event["训练窗口开始"]}</td>
                    <td>{event["训练窗口结束"]}</td>
                  </tr>
                ))}
                {events.length === 0 && (
                  <tr><td className="empty-state" colSpan={9}>该批次没有异常事件，可在下方事件总数里核对正常点</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <footer className="page-foot">
        <span>共 {total} 条异常检测记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
