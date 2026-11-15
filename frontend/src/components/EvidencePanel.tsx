import { useCallback, useEffect, useState } from 'react'

import type { Incident } from '../types/Incident'
import type {
  ClusterEntry,
  EvidenceCluster,
  EvidenceResponse,
  LogUploadResult,
} from '../types/Evidence'

type EvidencePanelProps = {
  apiUrl: string
  token: string
  incident: Incident
}

const SCORE_LABELS: Record<string, string> = {
  terms: 'Keyword overlap',
  service: 'Service match',
  level: 'Log severity',
  proximity: 'Closeness to start',
  frequency: 'Repetition',
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'unknown'
}

async function readError(response: Response, fallback: string) {
  try {
    const data = await response.json()
    if (typeof data.detail === 'string') return data.detail
  } catch {
    // body was not JSON
  }
  return fallback
}

function EvidencePanel({ apiUrl, token, incident }: EvidencePanelProps) {
  const [evidence, setEvidence] = useState<EvidenceResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const [beforeMinutes, setBeforeMinutes] = useState(15)
  const [afterMinutes, setAfterMinutes] = useState(60)

  const [file, setFile] = useState<File | null>(null)
  // Changing the key remounts the file input so it forgets the chosen file.
  const [fileInputKey, setFileInputKey] = useState(0)
  const [uploading, setUploading] = useState(false)
  const [uploadMessage, setUploadMessage] = useState('')
  const [uploadError, setUploadError] = useState('')

  const [openCluster, setOpenCluster] = useState<string | null>(null)
  const [entries, setEntries] = useState<ClusterEntry[]>([])
  const [entriesLoading, setEntriesLoading] = useState(false)

  const authHeaders = { Authorization: `Bearer ${token}` }
  const windowQuery = `before_minutes=${beforeMinutes}&after_minutes=${afterMinutes}`

  const loadEvidence = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const response = await fetch(
        `${apiUrl}/incidents/${incident.id}/logs?${windowQuery}`,
        { headers: { Authorization: `Bearer ${token}` } }
      )
      if (!response.ok) {
        throw new Error(await readError(response, 'Could not load evidence'))
      }
      setEvidence(await response.json())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load evidence')
    } finally {
      setLoading(false)
    }
  }, [apiUrl, incident.id, token, windowQuery])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadEvidence()
  }, [loadEvidence])

  async function uploadLogs(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file) return

    setUploading(true)
    setUploadError('')
    setUploadMessage('')

    const form = new FormData()
    form.append('file', file)

    try {
      const response = await fetch(`${apiUrl}/incidents/${incident.id}/logs`, {
        method: 'POST',
        headers: authHeaders,
        body: form,
      })
      if (!response.ok) {
        throw new Error(await readError(response, 'Upload failed'))
      }
      const result: LogUploadResult = await response.json()
      setUploadMessage(
        result.duplicate
          ? 'That exact file was already uploaded.'
          : `Parsed ${result.log_entries_created} log lines` +
              (result.services_detected?.length
                ? ` from ${result.services_detected.join(', ')}.`
                : '.')
      )
      setFile(null)
      setFileInputKey((k) => k + 1)
      await loadEvidence()
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  async function toggleCluster(cluster: EvidenceCluster) {
    if (openCluster === cluster.cluster_id) {
      setOpenCluster(null)
      return
    }
    setOpenCluster(cluster.cluster_id)
    setEntries([])
    setEntriesLoading(true)
    try {
      const response = await fetch(
        `${apiUrl}/incidents/${incident.id}/logs/clusters/${cluster.cluster_id}/entries?${windowQuery}`,
        { headers: authHeaders }
      )
      if (response.ok) {
        const data = await response.json()
        setEntries(data.entries)
      }
    } finally {
      setEntriesLoading(false)
    }
  }

  const maxBucket = Math.max(1, ...(evidence?.timeline ?? []).map((b) => b.count))

  return (
    <section className="evidence-section">
      <h2>Log Evidence</h2>
      <p className="evidence-intro">
        Upload logs for this incident. LogLens ranks the log events that are
        most likely connected to it and shows why each one matched.
      </p>

      <form className="evidence-upload" onSubmit={uploadLogs}>
        <input
          key={fileInputKey}
          type="file"
          accept=".log,.txt,.json,.jsonl,.ndjson"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          aria-label="Log file"
        />
        <button type="submit" disabled={!file || uploading}>
          {uploading ? 'Uploading...' : 'Upload logs'}
        </button>
      </form>
      <p className="evidence-hint">
        JSON, JSONL or plain text, up to 5 MB. Files are parsed and not kept.
      </p>
      {uploadMessage && <p className="success-message">{uploadMessage}</p>}
      {uploadError && <p className="error-message">{uploadError}</p>}

      <div className="evidence-window">
        <label>
          Minutes before start
          <input
            type="number"
            min={0}
            max={1440}
            value={beforeMinutes}
            onChange={(e) => setBeforeMinutes(Number(e.target.value) || 0)}
          />
        </label>
        <label>
          Minutes after start
          <input
            type="number"
            min={0}
            max={1440}
            value={afterMinutes}
            onChange={(e) => setAfterMinutes(Number(e.target.value) || 0)}
          />
        </label>
      </div>

      {loading && !evidence && <p>Matching logs...</p>}
      {error && <p className="error-message">{error}</p>}

      {evidence && (
        <>
          <p className="evidence-summary">
            {evidence.window.applied
              ? `Searched ${formatTime(evidence.window.start)} to ${formatTime(evidence.window.end)}`
              : 'No start time on this incident, so only keyword and service matches are used'}
            {' · '}
            {evidence.candidates_scanned} candidate lines
            {evidence.services.length > 0 && ` · service: ${evidence.services.join(', ')}`}
          </p>
          {evidence.truncated && (
            <p className="error-message">
              Too many candidate lines; showing the most recent. Narrow the time window.
            </p>
          )}

          {evidence.timeline.length > 1 && (
            <div className="evidence-timeline" aria-label="Matched events over time">
              {evidence.timeline.map((bucket) => (
                <div
                  key={bucket.start}
                  className="timeline-bar"
                  title={`${formatTime(bucket.start)}: ${bucket.count} events, ${bucket.notable} warnings or errors`}
                >
                  <span
                    className={bucket.notable > 0 ? 'timeline-fill notable' : 'timeline-fill'}
                    style={{ height: `${(bucket.count / maxBucket) * 100}%` }}
                  />
                </div>
              ))}
            </div>
          )}

          {evidence.clusters.length === 0 ? (
            <p className="evidence-empty">
              No matching log events yet. Upload logs covering the incident time,
              or widen the time window.
            </p>
          ) : (
            <ol className="evidence-list">
              {evidence.clusters.map((cluster, index) => (
                <li key={cluster.cluster_id} className="evidence-card">
                  <div className="evidence-card-head">
                    <span className="evidence-rank">#{index + 1}</span>
                    <span className={`evidence-level level-${cluster.level.toLowerCase()}`}>
                      {cluster.level}
                    </span>
                    {cluster.service && (
                      <span className="evidence-service">{cluster.service}</span>
                    )}
                    <span className="evidence-score">score {cluster.score}</span>
                  </div>

                  <p className="evidence-message">{cluster.message}</p>

                  <p className="evidence-meta">
                    {cluster.occurrence_count}× · {formatTime(cluster.first_seen)}
                    {cluster.occurrence_count > 1 && ` to ${formatTime(cluster.last_seen)}`}
                    {cluster.document_title && ` · ${cluster.document_title}`}
                  </p>

                  <ul className="evidence-why">
                    {cluster.why.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>

                  <div className="evidence-breakdown">
                    {Object.entries(cluster.score_breakdown)
                      .filter(([, value]) => value > 0)
                      .map(([key, value]) => (
                        <span key={key} className="evidence-chip">
                          {SCORE_LABELS[key] ?? key} +{value}
                        </span>
                      ))}
                  </div>

                  <button
                    type="button"
                    className="evidence-toggle"
                    onClick={() => toggleCluster(cluster)}
                  >
                    {openCluster === cluster.cluster_id ? 'Hide log lines' : 'Show log lines'}
                  </button>

                  {openCluster === cluster.cluster_id && (
                    <div className="evidence-lines">
                      {entriesLoading && <p>Loading...</p>}
                      {entries.map((entry) => (
                        <pre key={entry.id}>
                          {formatTime(entry.timestamp)} {entry.level ?? ''}{' '}
                          {entry.service ? `[${entry.service}] ` : ''}
                          {entry.text}
                        </pre>
                      ))}
                    </div>
                  )}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  )
}

export default EvidencePanel
