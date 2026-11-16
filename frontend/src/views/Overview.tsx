import { useEffect, useState } from 'react'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

type AnomalyEvent = Record<string, string | number | boolean | null>

const fallbackCards = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
const fallbackModules = [{"name": "实时指标", "created": 0, "pending": 0, "abnormal": 0}, {"name": "监控大盘", "created": 0, "pending": 0, "abnormal": 0}, {"name": "多维下钻", "created": 0, "pending": 0, "abnormal": 0}, {"name": "阈值管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "异常检测", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据服务", "created": 0, "pending": 0, "abnormal": 0}, {"name": "告警历史", "created": 0, "pending": 0, "abnormal": 0}, {"name": "同比环比", "created": 0, "pending": 0, "abnormal": 0}, {"name": "聚合配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "筛选预设", "created": 0, "pending": 0, "abnormal": 0}, {"name": "用户洞察", "created": 0, "pending": 0, "abnormal": 0}, {"name": "事件分析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "分群画像", "created": 0, "pending": 0, "abnormal": 0}, {"name": "趋势预测", "created": 0, "pending": 0, "abnormal": 0}, {"name": "可视化模板", "created": 0, "pending": 0, "abnormal": 0}, {"name": "订阅推送", "created": 0, "pending": 0, "abnormal": 0}, {"name": "维度映射", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据质量分", "created": 0, "pending": 0, "abnormal": 0}]

export default function Overview() {
  const [cards, setCards] = useState<Overview['cards']>(fallbackCards)
  const [moduleRows, setModuleRows] = useState<Overview['modules']>(fallbackModules)
  const [anomalyEvents, setAnomalyEvents] = useState<AnomalyEvent[]>([])

  useEffect(() => {
    fetchJson<Overview>('/api/overview')
      .then((payload) => {
        setCards(payload.cards)
        setModuleRows(payload.modules)
      })
      .catch(() => undefined)
    fetchJson<{ items: AnomalyEvent[] }>('/api/anomalydetect/events?only_anomaly=true')
      .then((payload) => setAnomalyEvents((payload.items ?? []).slice(-10).reverse()))
      .catch(() => undefined)
  }, [])

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>运营概览</h2>
          <p className="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
        </div>
      </header>
      <div className="stat-row">
        {cards.map((card) => (
          <article key={card.label} className="stat-card">
            <span className="stat-label">{card.label}</span>
            <strong className="stat-value">{card.value}</strong>
          </article>
        ))}
      </div>
      <table className="data-table">
        <thead>
          <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
        </thead>
        <tbody>
          {moduleRows.map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
              <td>{row.created}</td>
              <td>{row.pending}</td>
              <td>{row.abnormal}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="panel">
        <h3 className="panel-title">异常量来自哪次训练</h3>
        <p className="panel-desc">
          异常卡片的数字由异常检测事件数据集汇总而来；每条事件都带部署版本、检测算法与训练窗口，
          可以追到具体那次训练。
        </p>
        <table className="data-table">
          <thead>
            <tr>
              <th>事件编号</th><th>业务批次</th><th>指标值</th><th>严重度</th>
              <th>模型编号</th><th>检测算法</th><th>部署版本</th>
              <th>训练窗口开始</th><th>训练窗口结束</th>
            </tr>
          </thead>
          <tbody>
            {anomalyEvents.map((event) => (
              <tr key={String(event.id)}>
                <td>{String(event["事件编号"] ?? '—')}</td>
                <td>{String(event["业务批次"] ?? '—')}</td>
                <td>{String(event["指标值"] ?? '—')}</td>
                <td>{String(event["严重度"] ?? '—')}</td>
                <td>{String(event["模型编号"] ?? '—')}</td>
                <td>{String(event["检测算法"] ?? '—')}</td>
                <td>{String(event["部署版本"] ?? '—')}</td>
                <td>{String(event["训练窗口开始"] ?? '—')}</td>
                <td>{String(event["训练窗口结束"] ?? '—')}</td>
              </tr>
            ))}
            {anomalyEvents.length === 0 && (
              <tr><td className="empty-state" colSpan={9}>还没有异常事件，可在异常检测页部署模型后按业务批次生成</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
