export type Incident = {
  id: number
  title: string
  description: string | null
  status: string
  severity: string | null
  created_at: string
  creator_id: number
  service: string | null
  started_at: string | null
  resolved_at: string | null
  source: string
}

export type IncidentListResponse = {
  count: number
  incidents: Incident[]
}
