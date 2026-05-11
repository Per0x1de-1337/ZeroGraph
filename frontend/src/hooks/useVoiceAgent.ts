import { useCallback, useEffect, useRef, useState } from 'react';
import { AudioPlayer } from '../lib/AudioPlayer';
import { EnergyVAD } from '../lib/EnergyVAD';
import { ThinkingSound } from '../lib/ThinkingSound';
import type { AgentState, Metrics, Turn } from '../types';

export interface SessionStats {
  turns: number;
  interruptions: number;
  avgE2E: number | null;
}

interface VoiceAgentState {
  agentState: AgentState;
  turns: Turn[];
  metrics: Metrics;
  noiseFloor: number;
  vadThreshold: number;
  isRunning: boolean;
  analyser: AnalyserNode | null;
  session: SessionStats;
}

interface VoiceAgentActions {
  start: () => Promise<void>;
  stop: () => void;
}

let turnId = 0;

export function useVoiceAgent(): VoiceAgentState & VoiceAgentActions {
  const [agentState, setAgentState] = useState<AgentState>('IDLE');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [metrics, setMetrics] = useState<Metrics>({});
  const [noiseFloor, setNoiseFloor] = useState(0);
  const [vadThreshold, setVadThreshold] = useState(0);
  const [isRunning, setIsRunning] = useState(false);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);
  const [session, setSession] = useState<SessionStats>({ turns: 0, interruptions: 0, avgE2E: null });
  const e2eHistoryRef = useRef<number[]>([]);

  // All mutable runtime objects live in refs — never cause re-renders
  const wsRef       = useRef<WebSocket | null>(null);
  const playerRef   = useRef<AudioPlayer>(new AudioPlayer());
  const thinkingRef = useRef<ThinkingSound>(new ThinkingSound());
  const vadRef      = useRef<EnergyVAD>(new EnergyVAD());
  const micCtxRef   = useRef<AudioContext | null>(null);
  const workletRef  = useRef<AudioWorkletNode | null>(null);
  const pingRef     = useRef<ReturnType<typeof setInterval> | null>(null);
  const noiseMonRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const agentStateRef = useRef<AgentState>('IDLE');

  // Keep ref in sync with state so callbacks don't close over stale values
  useEffect(() => { agentStateRef.current = agentState; }, [agentState]);

  // ── WebSocket ────────────────────────────────────────────────────────────

  const sendJSON = useCallback((obj: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(obj));
    }
  }, []);

  const connectWS = useCallback(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;

    ws.onopen = () => {
      pingRef.current = setInterval(() => {
        sendJSON({ type: 'ping', t: Date.now() });
      }, 2000);
    };

    ws.onclose = () => {
      if (pingRef.current) clearInterval(pingRef.current);
      setAgentState('DISCONNECTED');
    };

    ws.onmessage = async ({ data }) => {
      if (data instanceof ArrayBuffer) {
        playerRef.current.push(data);
        return;
      }
      const msg = JSON.parse(data as string);
      switch (msg.type) {
        case 'state': {
          const s = msg.state as AgentState;
          setAgentState(s);
          if (s === 'PROCESSING') thinkingRef.current.start();
          else                    thinkingRef.current.stop();
          if (s === 'SPEAKING') {
            setTurns(prev => {
              // clear any in-progress agent turn
              const last = prev[prev.length - 1];
              if (last?.speaker === 'agent' && !last.final) return prev.slice(0, -1);
              return prev;
            });
          }
          break;
        }
        case 'interrupt':
          await playerRef.current.flush();
          setSession(s => ({ ...s, interruptions: s.interruptions + 1 }));
          break;
        case 'transcript':
          setTurns(prev => {
            const last = prev[prev.length - 1];
            if (last?.speaker === 'user' && !last.final) {
              return [...prev.slice(0, -1), { ...last, text: msg.text, final: msg.final }];
            }
            // Show "…" placeholder as a real turn immediately on VAD start
            if (msg.text === '…') {
              return [...prev, { id: ++turnId, speaker: 'user', text: '…', final: false }];
            }
            return [...prev, { id: ++turnId, speaker: 'user', text: msg.text, final: msg.final }];
          });
          break;
        case 'agent_token':
          setTurns(prev => {
            const last = prev[prev.length - 1];
            if (last?.speaker === 'agent' && !last.final) {
              return [...prev.slice(0, -1), { ...last, text: last.text + msg.token }];
            }
            return [...prev, { id: ++turnId, speaker: 'agent', text: msg.token, final: false }];
          });
          break;
        case 'agent_done':
          setTurns(prev => {
            const last = prev[prev.length - 1];
            if (last?.speaker === 'agent') return [...prev.slice(0, -1), { ...last, final: true }];
            return prev;
          });
          break;
        case 'metrics': {
          const { ttfp, ttft, ttfa } = msg as { ttfp?: number; ttft?: number; ttfa?: number };
          setMetrics(m => ({ ...m, ttfp, ttft, ttfa }));
          if (ttfp !== undefined && ttft !== undefined && ttfa !== undefined) {
            const e2e = ttfp + ttft + ttfa;
            e2eHistoryRef.current = [...e2eHistoryRef.current.slice(-19), e2e];
            const avg = e2eHistoryRef.current.reduce((a, b) => a + b, 0) / e2eHistoryRef.current.length;
            setSession(s => ({ ...s, turns: s.turns + 1, avgE2E: Math.round(avg) }));
          }
          break;
        }
        case 'pong':
          setMetrics(m => ({ ...m, rtt: Date.now() - msg.t }));
          break;
      }
    };
  }, [sendJSON]);

  // ── Microphone + Worklet ─────────────────────────────────────────────────

  const setupMic = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: 16000,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const ctx = new AudioContext({ sampleRate: 16000 });
    micCtxRef.current = ctx;

    const analyser = ctx.createAnalyser();
    analyser.fftSize = 128;
    analyserRef.current = analyser;
    setAnalyser(analyser);

    await ctx.audioWorklet.addModule('/worklet.js');
    const worklet = new AudioWorkletNode(ctx, 'pcm-processor');
    workletRef.current = worklet;

    const src = ctx.createMediaStreamSource(stream);
    src.connect(analyser);
    src.connect(worklet);

    const vad = vadRef.current;
    vad.onspeechstart = () => {
      sendJSON({ type: 'vad_start' });
      // Show instant "hearing you" dot before Deepgram's first partial
      if (agentStateRef.current === 'LISTENING' || agentStateRef.current === 'INTERRUPTED') {
        setTurns(prev => {
          const last = prev[prev.length - 1];
          if (last?.speaker === 'user' && !last.final) return prev;
          return [...prev, { id: ++turnId, speaker: 'user', text: '…', final: false }];
        });
      }
    };
    vad.onspeechend = () => sendJSON({ type: 'vad_end' });

    worklet.port.onmessage = ({ data }: MessageEvent<{ rms: number; pcm: ArrayBuffer }>) => {
      vad.feed(data.rms);
      const s = agentStateRef.current;
      if ((s === 'LISTENING' || s === 'INTERRUPTED') && wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(data.pcm);
      }
    };

    // Poll noise floor every 500ms for UI display
    noiseMonRef.current = setInterval(() => {
      setNoiseFloor(vad.floor);
      setVadThreshold(vad.threshold);
    }, 500);
  }, [sendJSON]);

  // ── Public API ───────────────────────────────────────────────────────────

  const start = useCallback(async () => {
    await playerRef.current.init();
    await setupMic();
    connectWS();
    setIsRunning(true);
  }, [setupMic, connectWS]);

  const stop = useCallback(() => {
    wsRef.current?.close();
    micCtxRef.current?.close();
    if (pingRef.current) clearInterval(pingRef.current);
    if (noiseMonRef.current) clearInterval(noiseMonRef.current);
    setIsRunning(false);
    setAgentState('IDLE');
    setTurns([]);
    setSession({ turns: 0, interruptions: 0, avgE2E: null });
    e2eHistoryRef.current = [];
  }, []);

  // Cleanup on unmount
  useEffect(() => () => stop(), [stop]);

  return { agentState, turns, metrics, noiseFloor, vadThreshold, isRunning, analyser, session, start, stop };
}
