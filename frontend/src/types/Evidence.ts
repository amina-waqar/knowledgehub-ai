export type ScoreBreakdown = {
  terms: number
  service: number
  level: number
  proximity: number
  frequency: number
}

export type EvidenceExample = {
  id: number
  timestamp: string | null
  level: string | null
  service: string | null
  text: string
}

export type EvidenceCluster = {
  cluster_id: string
  template: string
  message: string
  level: string
  service: string | null
  occurrence_count: number
  first_seen: string | null
  last_seen: string | null
  document_id: number | null
  document_title: string | null
  trace_ids: string[]
  matching_terms: string[]
  score: number
  score_breakdown: ScoreBreakdown
  why: string[]
  examples: EvidenceExample[]
}

export type TimelineBucket = {
  start: string
  count: number
  notable: number
}

export type EvidenceResponse = {
  incident_id: number
  source: string
  window: {
    start: string | null
    end: string | null
    anchor: string | null
    applied: boolean
  }
  services: string[]
  terms: string[]
  candidates_scanned: number
  truncated: boolean
  clusters: EvidenceCluster[]
  timeline: TimelineBucket[]
}

export type ClusterEntry = {
  id: number
  timestamp: string | null
  level: string | null
  service: string | null
  trace_id: string | null
  host: string | null
  text: string
}

export type LogUploadResult = {
  id: number
  title: string
  duplicate: boolean
  log_entries_created: number
  entries_with_timestamp?: number
  services_detected?: string[]
  message?: string
}
