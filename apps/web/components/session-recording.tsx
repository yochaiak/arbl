"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { api, request } from "@/lib/api";
import { type BrowserSession, platformKey } from "@/lib/platform";
import type { components } from "@/lib/schema";
import { Modal } from "./platform-ui";
import { Empty, ErrorNotice, Field, Loading, Select } from "./ui";

type RecordingManifest = components["schemas"]["SessionRecordingManifest"];
type RecordingAttempt = RecordingManifest["attempts"][number];
type RecordingSession = Pick<BrowserSession, "id" | "recording_status" | "recording_available">;

const recordingLabels = {
  recording: "Recording",
  ready: "Ready",
  partial: "Partial",
  unavailable: "Unavailable",
  expired: "Expired",
} satisfies Record<NonNullable<BrowserSession["recording_status"]>, string>;

export function RecordingAction({ session, onPlay, compact = false }: {
  session: RecordingSession;
  onPlay: (id: string) => void;
  compact?: boolean;
}) {
  const status = recordingLabels[session.recording_status ?? "unavailable"];
  const available = session.recording_available === true;
  const label = `Play recording for session ${session.id}`;
  return (
    <div className={compact ? "session-recording-action compact" : "session-recording-action"}>
      <button
        type="button"
        className={compact ? "icon-button" : "button secondary"}
        aria-label={label}
        title={available ? "Play recording" : status}
        disabled={!available}
        onClick={() => onPlay(session.id)}
      >
        <Play size={compact ? 14 : 16} aria-hidden="true" />
        {!compact && "Play recording"}
      </button>
      <span className="muted">{status}</span>
    </div>
  );
}

function RecordingClip({ path, token, onEnded }: {
  path: string;
  token: string;
  onEnded: () => void;
}) {
  const [source, setSource] = useState<string>();
  const [error, setError] = useState<unknown>();
  useEffect(() => {
    const abort = new AbortController();
    let url: string | undefined;

    async function load() {
      try {
        const response = await request(path, token, { signal: abort.signal });
        const video = await response.blob();
        if (abort.signal.aborted) return;
        url = URL.createObjectURL(video);
        setSource(url);
      } catch (error) {
        if (!abort.signal.aborted) setError(error);
      }
    }

    queueMicrotask(() => {
      if (!abort.signal.aborted) void load();
    });
    return () => {
      abort.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [path, token]);

  if (error) return <ErrorNotice error={error} />;
  if (!source) return <p role="status" className="loading"><span className="spinner" /> Loading recording clip…</p>;
  return (
    <video
      className="session-recording-video"
      aria-label="Session recording video"
      src={source}
      controls
      autoPlay
      muted
      playsInline
      onEnded={onEnded}
      onError={() => setError(new Error("This recording clip could not be played."))}
    />
  );
}

function clipTime(startMs: number) {
  const seconds = Math.floor(startMs / 1000);
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function AttemptRecording({ token, path, attempt }: {
  token: string;
  path: string;
  attempt: RecordingAttempt;
}) {
  const [clipIndex, setClipIndex] = useState(0);
  const currentIndex = Math.min(clipIndex, attempt.segments.length - 1);
  const clip = attempt.segments[currentIndex];
  if (!clip) return null;

  return (
    <>
      {attempt.segments.length > 1 && (
        <Field label="Recording clip">
          <Select value={String(currentIndex)} onChange={(event) => setClipIndex(Number(event.target.value))}>
            {attempt.segments.map((segment, index) => (
              <option value={index} key={segment.sequence}>Clip {index + 1} · {clipTime(segment.start_ms)}</option>
            ))}
          </Select>
        </Field>
      )}
      {attempt.status === "partial" && <p className="muted small-text">This recording contains only the clips that could be saved.</p>}
      <RecordingClip
        key={clip.sequence}
        path={`${path}/${attempt.attempt_number}/segments/${clip.sequence}`}
        token={token}
        onEnded={() => setClipIndex(Math.min(currentIndex + 1, attempt.segments.length - 1))}
      />
    </>
  );
}

export function SessionRecordingPlayer({ token, sessionId, close }: {
  token: string;
  sessionId: string;
  close: () => void;
}) {
  const [attemptNumber, setAttemptNumber] = useState<number>();
  const path = `/sessions/${encodeURIComponent(sessionId)}/recordings`;
  const manifest = useQuery({
    queryKey: platformKey(token, "session-recordings", sessionId),
    queryFn: ({ signal }) => api<RecordingManifest>(path, token, { signal }),
    retry: false,
    gcTime: 0,
  });
  const attempts = manifest.data?.attempts.filter((attempt) => attempt.segments.length > 0) ?? [];
  const current = attempts.find((attempt) => attempt.attempt_number === attemptNumber) ?? attempts[0];
  const expired = manifest.data?.attempts.some((attempt) => attempt.status === "expired");

  return (
    <Modal title="Session recording" detail={sessionId.slice(0, 12)} close={close} wide>
      <div className="modal-body stack">
        {manifest.isPending ? <Loading /> : manifest.error ? <ErrorNotice error={manifest.error} /> : current ? (
          <>
            <Field label="Recording attempt">
              <Select value={String(current.attempt_number)} onChange={(event) => setAttemptNumber(Number(event.target.value))}>
                {attempts.map((attempt) => (
                  <option value={attempt.attempt_number} key={attempt.attempt_number}>
                    Attempt {attempt.attempt_number} · {recordingLabels[attempt.status]}
                  </option>
                ))}
              </Select>
            </Field>
            <AttemptRecording key={current.attempt_number} token={token} path={path} attempt={current} />
          </>
        ) : (
          <Empty
            title={expired ? "Recording expired" : "Recording unavailable"}
            detail={expired ? "This recording has reached the end of its retention period." : "No playable clips are available for this session."}
          />
        )}
      </div>
    </Modal>
  );
}
