const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

export interface HealthResponse {
  status: string;
  version: string;
  timestamp: string;
}

export async function getHealth(): Promise<HealthResponse> {
  const r = await fetch(`${API_URL}/health`);
  if (!r.ok) {
    throw new Error(`Health check failed: HTTP ${r.status}`);
  }
  return r.json() as Promise<HealthResponse>;
}

// === Copilot ===

export interface SuggestedContract {
  underlying: string;
  occ_symbol: string;
  option_type: 'call' | 'put';
  strike: string;
  expiration: string;
  estimated_premium_per_contract: string;
  contracts: number;
  max_risk_usd: string;
}

export interface Thesis {
  symbol: string;
  direction: 'long' | 'short';
  confidence: number;
  reasoning: string;
  prediction_window_days: number;
  suggested_contract: SuggestedContract;
  what_must_happen: string;
  correlation_id: string;
  source_bucket: string;
  generated_at: string;
  grounding_check_passed: boolean;
  grounding_notes: string | null;
  llm_provider: string;
  llm_model: string;
  funnel_latency_ms: number;
}

export interface ThesisRequest {
  user_thesis: string;
  risk_budget_usd: number | null;
}

export async function postThesis(req: ThesisRequest): Promise<Thesis> {
  const r = await fetch(`${API_URL}/copilot/thesis`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        detail = `${detail}: ${JSON.stringify(body.detail)}`;
      }
    } catch {
      /* fall through */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<Thesis>;
}

// === Autonomous ===

export interface TriageDecisionRecord {
  event_id: string;
  symbol: string | null;
  headline: string;
  body_excerpt?: string | null;
  url?: string | null;
  passed: boolean;
  reason: string;
  confidence: number;
  at: string;
  source?: 'edgar' | 'alpaca-news' | 'rss' | 'flag-scanner';
  kind?: 'filing' | 'news' | 'scan';
}

export interface WorkerStatus {
  state: 'stopped' | 'starting' | 'running' | 'stopping';
  started_at: string | null;
  stopped_at: string | null;
  watchlist: string[];
  poll_interval_seconds: number;
  triage_provider: string;
  triage_model: string;
  thesis_provider: string;
  thesis_model: string;
  polls_completed: number;
  events_published: number;
  events_consumed: number;
  events_passed_triage: number;
  theses_produced: number;
  triage_failures: number;
  thesis_failures: number;
  persistence_failures: number;
  queue_depth: number;
  last_event_at: string | null;
  last_thesis_at: string | null;
  last_poll_at: string | null;
  last_error: string | null;
  recent_triage_decisions: TriageDecisionRecord[];
}

export interface RecentThesis {
  id: number;
  symbol: string;
  direction: 'long' | 'short';
  confidence: number;
  source_bucket: string;
  generated_at: string;
  grounding_check_passed: boolean;
  reasoning: string;
  suggested_contract: Record<string, unknown> | null;
  llm_provider: string;
  llm_model: string;
  funnel_latency_ms: number | null;
  correlation_id: string;
}

async function _request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, init);
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body && typeof body === 'object' && 'detail' in body) {
        detail = `${detail}: ${JSON.stringify(body.detail)}`;
      }
    } catch {
      /* fall through */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

export async function getAutonomousStatus(): Promise<WorkerStatus> {
  return _request<WorkerStatus>('/autonomous/status');
}

export async function startAutonomous(): Promise<WorkerStatus> {
  return _request<WorkerStatus>('/autonomous/start', { method: 'POST' });
}

export async function stopAutonomous(): Promise<WorkerStatus> {
  return _request<WorkerStatus>('/autonomous/stop', { method: 'POST' });
}

export async function getRecentReactiveTheses(limit = 10): Promise<RecentThesis[]> {
  return _request<RecentThesis[]>(`/autonomous/theses?limit=${limit}`);
}

export interface UpcomingCatalyst {
  id: number;
  symbol: string;
  event_type: string;
  event_date: string; // ISO date
  event_hour: string | null;
  estimated_eps: string | null;
  state: 'scheduled' | 'triggered' | 'expired';
  thesis_id: number | null;
  days_until: number;
}

export async function getUpcomingCatalysts(within_days = 14): Promise<UpcomingCatalyst[]> {
  return _request<UpcomingCatalyst[]>(`/autonomous/catalysts?within_days=${within_days}`);
}

export interface InjectEventRequest {
  symbol: string;
  headline: string;
  kind?: 'news' | 'filing' | 'scan';
  body?: string | null;
}

export interface InjectEventResponse {
  event_id: string;
  accepted: boolean;
}

export async function injectSyntheticEvent(req: InjectEventRequest): Promise<InjectEventResponse> {
  return _request<InjectEventResponse>('/autonomous/inject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
}

// === Options chain (Phase 3 UI) ===

export interface OptionRow {
  occ_symbol: string;
  expiration: string;
  strike: string;
  option_type: 'call' | 'put';
  bid: string | null;
  ask: string | null;
  last: string | null;
  mid: string | null;
}

export interface ChainResponse {
  symbol: string;
  expiration: string;
  underlying_price: string | null;
  calls: OptionRow[];
  puts: OptionRow[];
}

export interface ExpirationsResponse {
  symbol: string;
  expirations: string[];
}

export async function getExpirations(symbol: string): Promise<ExpirationsResponse> {
  return _request<ExpirationsResponse>(`/chain/${encodeURIComponent(symbol)}/expirations`);
}

export async function getChain(symbol: string, expiration: string): Promise<ChainResponse> {
  return _request<ChainResponse>(
    `/chain/${encodeURIComponent(symbol)}?expiration=${encodeURIComponent(expiration)}`,
  );
}

// === Portfolio (Phase 7 shadow mode) ===

export interface AccountSummary {
  id: number;
  kind: string;
  name: string;
  starting_balance_usd: string;
  equity_usd: string;
  min_confidence: number;
  max_trade_cost_usd: string;
  max_trades_per_day: number;
  max_concurrent_positions: number;
  kill_switch: boolean;
  open_shadow_positions: number;
  shadow_trades_today: number;
  shadow_trades_total: number;
  total_cost_open_usd: string;
}

export interface ShadowTradeRow {
  id: number;
  account_id: number;
  account_kind: string;
  thesis_id: number;
  opened_at: string;
  underlying: string;
  occ_symbol: string;
  option_type: 'call' | 'put';
  strike: string;
  expiration: string;
  contracts: number;
  premium_per_contract_usd: string;
  total_cost_usd: string;
  status: string;
  risk_reason: string;
  closed_at: string | null;
  close_reason: string | null;
  realized_pnl_usd: string | null;
  /** Latest mark-to-market for open positions (null until the first MTM tick). */
  unrealized_pnl_usd: string | null;
  marked_at: string | null;
}

export async function getPaperAccounts(): Promise<AccountSummary[]> {
  return _request<AccountSummary[]>('/portfolio/accounts');
}

export async function getShadowTrades(
  account_kind?: string,
  limit = 20,
): Promise<ShadowTradeRow[]> {
  const qs = account_kind
    ? `?account_kind=${encodeURIComponent(account_kind)}&limit=${limit}`
    : `?limit=${limit}`;
  return _request<ShadowTradeRow[]>(`/portfolio/shadow-trades${qs}`);
}

export interface EquityPoint {
  t: string;
  equity: string;
  open_unrealized: string;
  closed_realized: string;
}

export interface EquityCurveResponse {
  account_kind: string;
  starting_balance_usd: string;
  points: EquityPoint[];
}

export async function getEquityCurve(
  account_kind: string,
  days = 30,
): Promise<EquityCurveResponse> {
  return _request<EquityCurveResponse>(
    `/portfolio/accounts/${encodeURIComponent(account_kind)}/equity-curve?days=${days}`,
  );
}

// === Eval ===

export type SourceBucket = 'manual' | 'reactive' | 'catalyst';

export interface BucketSummary {
  bucket: SourceBucket;
  count_theses: number;
  count_resolved: number;
  brier: number | null;
  hit_rate: number | null;
}

export interface EvalSummary {
  buckets: BucketSummary[];
}

export interface CalibrationPoint {
  bucket_lower: number;
  bucket_upper: number;
  count: number;
  mean_confidence: number;
  realized_hit_rate: number;
}

export interface CalibrationResponse {
  bucket: SourceBucket;
  n_buckets: number;
  points: CalibrationPoint[];
}

export async function getEvalSummary(): Promise<EvalSummary> {
  return _request<EvalSummary>('/eval/summary');
}

export async function getCalibration(
  bucket: SourceBucket = 'manual',
  n_buckets = 10,
): Promise<CalibrationResponse> {
  return _request<CalibrationResponse>(`/eval/calibration?bucket=${bucket}&n_buckets=${n_buckets}`);
}

export interface OutcomeRow {
  thesis_id: number;
  symbol: string;
  source_bucket: SourceBucket;
  direction: 'long' | 'short';
  confidence: number;
  generated_at: string;
  evaluated_at: string;
  realized_direction: string;
  hit: boolean;
  pct_move: number;
  underlying_price_at_thesis: number;
  underlying_price_at_eval: number;
  notes: string | null;
}

export async function getEvalOutcomes(bucket?: SourceBucket, limit = 25): Promise<OutcomeRow[]> {
  const qs = bucket ? `?bucket=${bucket}&limit=${limit}` : `?limit=${limit}`;
  return _request<OutcomeRow[]>(`/eval/outcomes${qs}`);
}

// === LLM cost ===

export interface CostBreakdown {
  provider: string;
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface CostSummary {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  since: string;
  by_model: CostBreakdown[];
  daily_request_budget: number;
}

export async function getCostSummary(): Promise<CostSummary> {
  return _request<CostSummary>('/llm/cost-summary');
}

// === Ticker info + news ===

export interface TickerInfo {
  symbol: string;
  long_name: string | null;
  short_name: string | null;
  sector: string | null;
  industry: string | null;
  quote_type: string | null;
  market_cap_usd: number | null;
  employees: number | null;
  long_business_summary: string | null;
  website: string | null;
  fifty_two_week_high: string | null;
  fifty_two_week_low: string | null;
  next_earnings_date: string | null;
  next_earnings_state: string | null;
  next_earnings_thesis_id: number | null;
}

export interface TickerNewsRow {
  event_id: string;
  symbol: string | null;
  headline: string;
  body_excerpt: string | null;
  url: string | null;
  source: string;
  kind: string;
  passed: boolean;
  reason: string;
  confidence: number;
  decided_at: string;
  published_at: string;
}

export interface TickerNewsResponse {
  symbol: string;
  rows: TickerNewsRow[];
}

export async function getTickerInfo(symbol: string): Promise<TickerInfo> {
  return _request<TickerInfo>(`/tickers/${encodeURIComponent(symbol)}/info`);
}

export async function getTickerNews(symbol: string, limit = 15): Promise<TickerNewsResponse> {
  return _request<TickerNewsResponse>(`/tickers/${encodeURIComponent(symbol)}/news?limit=${limit}`);
}

// === Bars (chart pane) ===

export interface BarPoint {
  t: string;
  o: string;
  h: string;
  low: string;
  c: string;
  v: number;
}

export interface BarsResponse {
  symbol: string;
  timeframe: string;
  bars: BarPoint[];
}

export async function getBars(
  symbol: string,
  timeframe = '1Day',
  days = 90,
): Promise<BarsResponse> {
  return _request<BarsResponse>(
    `/bars/${encodeURIComponent(symbol)}?timeframe=${timeframe}&days=${days}`,
  );
}

export interface BatchBarsResponse {
  timeframe: string;
  series: Record<string, BarPoint[]>;
  unavailable: string[];
}

export async function getBarsBatch(
  symbols: string[],
  timeframe = '1Day',
  days = 30,
): Promise<BatchBarsResponse> {
  const q = symbols.join(',');
  return _request<BatchBarsResponse>(
    `/bars/batch/series?symbols=${encodeURIComponent(q)}&timeframe=${timeframe}&days=${days}`,
  );
}

// === Copilot SSE ===

export type CopilotEventKind =
  | 'started'
  | 'thinking'
  | 'tool_call'
  | 'tool_result'
  | 'thesis_validating'
  | 'thesis_rejected'
  | 'thesis_accepted'
  | 'grounding_check'
  | 'grounding_retry'
  | 'fallback_forced'
  | 'done'
  | 'error';

export interface CopilotStreamEvent {
  kind: CopilotEventKind;
  payload: Record<string, unknown>;
}

/**
 * POST to /copilot/thesis/stream and yield parsed SSE events as they arrive.
 * EventSource doesn't support POST, so we use fetch+ReadableStream and parse
 * the SSE wire format ourselves. Stops when the server closes the stream.
 */
export async function* streamThesis(
  req: ThesisRequest,
  signal?: AbortSignal,
): AsyncGenerator<CopilotStreamEvent, void, void> {
  const r = await fetch(`${API_URL}/copilot/thesis/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
    signal,
  });
  if (!r.ok || !r.body) {
    throw new Error(`stream failed: HTTP ${r.status}`);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE records are separated by a blank line. Process complete ones.
    let sepIdx: number;
    while ((sepIdx = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);
      const evt = parseSseRecord(raw);
      if (evt) yield evt;
    }
  }
  // Flush any trailing record (server might not always emit the final \n\n).
  if (buffer.trim().length > 0) {
    const evt = parseSseRecord(buffer);
    if (evt) yield evt;
  }
}

function parseSseRecord(raw: string): CopilotStreamEvent | null {
  let kind: string | null = null;
  let dataLine = '';
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) kind = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLine += line.slice(5).trim();
  }
  if (!kind) return null;
  try {
    const payload = dataLine ? (JSON.parse(dataLine) as Record<string, unknown>) : {};
    return { kind: kind as CopilotEventKind, payload };
  } catch {
    return null;
  }
}
