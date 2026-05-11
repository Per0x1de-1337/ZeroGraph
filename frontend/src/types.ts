export type AgentState =
  | 'IDLE'
  | 'LISTENING'
  | 'PROCESSING'
  | 'SPEAKING'
  | 'INTERRUPTED'
  | 'DISCONNECTED';

export interface Metrics {
  ttfp?: number;
  ttft?: number;
  ttfa?: number;
  rtt?: number;
}

export interface Turn {
  id: number;
  speaker: 'user' | 'agent';
  text: string;
  final: boolean;
}
