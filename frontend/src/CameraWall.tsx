import { useEffect, useId, useRef, useState } from "react";
import type { Camera, Snapshot, ZoneId } from "./types";
import {
  apiAssetUrl, matchingObservation, observationAge, observationIdentity,
  seekTarget, sourceTime, validDetections, ZONE_NAMES,
} from "./media";
import "./cameras.css";

export interface CameraWallProps {
  snapshot: Snapshot;
  selectedZone: ZoneId;
  onSelectZone: (id: ZoneId) => void;
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(() =>
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduced;
}

function AssetMessage({ title, detail }: { title: string; detail: string }) {
  return <div className="camera-empty" role="status">
    <span className="camera-empty-mark" aria-hidden="true">◎</span>
    <strong>{title}</strong><span>{detail}</span>
  </div>;
}

function MovingFeed({ camera, snapshot, paused }: {
  camera: Camera; snapshot: Snapshot; paused: boolean;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const positioned = useRef(false);
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const uri = apiAssetUrl(camera.mediaUri, camera.id, "video", window.location.origin, camera.variant);
  const shouldPlay = snapshot.scenario.state === "running" && !paused;
  const sync = () => {
    const element = video.current;
    if (!element || element.readyState < 1) return;
    const duration = Number.isFinite(element.duration) ? element.duration : camera.durationSeconds;
    if (!paused || !positioned.current) {
      const target = seekTarget(element.currentTime, camera.sourceSeconds, duration);
      if (target !== null) element.currentTime = target;
      positioned.current = true;
    }
    try {
      if (Number.isFinite(snapshot.scenario.speed) && snapshot.scenario.speed > 0) {
        element.playbackRate = snapshot.scenario.speed;
      }
    } catch {
      element.pause();
      setBlocked(true);
      return;
    }
    if (shouldPlay) {
      void element.play().then(() => setBlocked(false)).catch(() => setBlocked(true));
    } else {
      element.pause();
      setBlocked(false);
    }
  };
  useEffect(() => { sync(); }, [
    camera.sourceSeconds, camera.durationSeconds, snapshot.scenario.speed,
    snapshot.scenario.state, paused, attempt,
  ]);
  useEffect(() => {
    setFailed(false);
    setReady(false);
    positioned.current = false;
  }, [snapshot.services.footageReady]);
  useEffect(() => () => { video.current?.pause(); }, []);

  if (!snapshot.services.footageReady) return <AssetMessage title="Preparando a loja em movimento"
    detail="O serviço ainda não disponibilizou os vídeos sintéticos. Esta perspectiva aparecerá quando estiver pronta." />;
  if (!uri) return <AssetMessage title="Vídeo indisponível" detail="A fonte não corresponde à API autenticada da loja." />;
  if (failed) return <div className="camera-asset-state">
    <AssetMessage title="A imagem ainda não chegou" detail="O vídeo sintético pode estar em preparação ou temporariamente indisponível." />
    <button className="camera-button" onClick={() => {
      setFailed(false); setReady(false); positioned.current = false; setAttempt((value) => value + 1);
    }}>Tentar novamente</button>
  </div>;
  return <>
    <video key={`${uri}:${attempt}`} ref={video} className="camera-video" src={uri}
      muted playsInline loop preload="metadata" aria-label={`${camera.name} — vídeo sintético 3D`}
      onLoadedMetadata={sync} onLoadedData={() => setReady(true)} onError={() => setFailed(true)} />
    {!ready && <div className="camera-loading">
      <AssetMessage title="Preparando esta perspectiva" detail="Aguardando o vídeo sintético da loja." />
    </div>}
    {blocked && shouldPlay && <div className="camera-play-prompt">
      <button className="camera-button" onClick={sync}>Reproduzir {camera.name}</button>
      <span>A reprodução automática não pôde começar.</span>
    </div>}
    {paused && <span className="camera-media-status">Vídeo pausado neste dispositivo</span>}
    {!paused && snapshot.scenario.state === "paused" &&
      <span className="camera-media-status">Cenário pausado</span>}
  </>;
}

function AnalysisFrame({ camera, snapshot }: { camera: Camera; snapshot: Snapshot }) {
  const [asset, setAsset] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [dimensions, setDimensions] = useState({ width: 1, height: 1 });
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const currentSnapshot = useRef(snapshot);
  currentSnapshot.current = snapshot;
  const identity = observationIdentity(camera.vision);
  const uri = apiAssetUrl(camera.vision.frameUri, camera.id, "frame", window.location.origin);
  const eligible = camera.vision.status === "ready" && camera.vision.cameraId === camera.id &&
    camera.vision.sourceSeconds !== null && Number.isFinite(camera.vision.sourceSeconds) &&
    camera.vision.observedAt !== null && !!uri;

  useEffect(() => {
    if (!eligible || !uri) return;
    const controller = new AbortController();
    const reader = new FileReader();
    setAsset(null);
    setLoaded(false);
    setError(null);
    const load = async () => {
      try {
        // The API binds the image bytes to this observation, not a later frame.
        const response = await fetch(`${uri}?observation=${encodeURIComponent(camera.vision.id)}&attempt=${attempt}`, {
          credentials: "same-origin", cache: "no-store", signal: controller.signal, redirect: "error",
        });
        if (!response.ok || !response.headers.get("content-type")?.startsWith("image/")) {
          throw new Error("O quadro analisado está indisponível.");
        }
        if (response.headers.get("X-Vitrine-Observation-Id") !== camera.vision.id) {
          throw new Error("O serviço não confirmou a correspondência entre imagem e detecção.");
        }
        const blob = await response.blob();
        const imageData = await new Promise<string>((resolve, reject) => {
          reader.onload = () => resolve(String(reader.result));
          reader.onerror = () => reject(new Error("Não foi possível abrir o quadro analisado."));
          reader.onabort = () => reject(new Error("Carregamento cancelado."));
          reader.readAsDataURL(blob);
        });
        if (!matchingObservation(currentSnapshot.current, camera, snapshot.scenario)) {
          throw new Error("A análise mudou. Aguarde o próximo quadro ou tente novamente.");
        }
        if (controller.signal.aborted) return;
        setAsset(imageData);
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Quadro indisponível.");
        }
      }
    };
    void load();
    return () => {
      controller.abort();
      if (reader.readyState === FileReader.LOADING) reader.abort();
    };
  }, [identity, uri, eligible, attempt, camera.variant, snapshot.scenario.id, snapshot.scenario.activeSince]);

  if (!eligible) return <AssetMessage
    title={camera.vision.status === "warming" ? "Preparando a primeira análise" : "Análise indisponível"}
    detail="As detecções só aparecem sobre o quadro correspondente, nunca sobre o vídeo em movimento." />;
  if (error) return <div className="camera-asset-state">
    <AssetMessage title="Aguardando um quadro confirmado" detail={error} />
    <button className="camera-button" onClick={() => setAttempt((value) => value + 1)}>Recarregar análise</button>
  </div>;
  const boxes = validDetections(camera.vision.detections);
  return <div className="camera-analysis">
    {!loaded && <AssetMessage title="Carregando o quadro analisado" detail="Confirmando imagem e observação antes de mostrar detecções." />}
    {asset && <div className={`camera-still${loaded ? " camera-still-ready" : ""}`}>
      <img src={asset} alt={`${camera.name}: quadro analisado em ${sourceTime(camera.vision.sourceSeconds)} do vídeo`}
        onLoad={(event) => {
          setDimensions({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight });
          setLoaded(true);
        }} onError={() => { setLoaded(false); setError("Não foi possível abrir a imagem analisada."); }} />
      {loaded && <svg className="camera-detections" viewBox={`0 0 ${dimensions.width} ${dimensions.height}`}
        preserveAspectRatio="xMidYMid meet"
        role="img" aria-label={`${boxes.length} caixas de detecção de pessoas neste quadro`}>
        {boxes.map((detection, index) => {
          const [x1, y1, x2, y2] = detection.box;
          return <g key={index}>
            <title>{`Pessoa ${index + 1} · confiança ${(detection.score * 100).toFixed(1)}%`}</title>
            <rect x={x1 * dimensions.width} y={y1 * dimensions.height}
              width={(x2 - x1) * dimensions.width} height={(y2 - y1) * dimensions.height} />
          </g>;
        })}
      </svg>}
    </div>}
  </div>;
}

function ObservationDetails({ camera, snapshot }: { camera: Camera; snapshot: Snapshot }) {
  const vision = camera.vision;
  const age = observationAge(vision.observedAt, snapshot.serverTime);
  const detections = validDetections(vision.detections);
  const confidence = detections.length
    ? `${(detections.reduce((sum, detection) => sum + detection.score, 0) / detections.length * 100).toFixed(1)}%`
    : "Indisponível";
  const time = vision.observedAt && Number.isFinite(Date.parse(vision.observedAt))
    ? new Date(vision.observedAt).toLocaleString("pt-BR") : "Indisponível";
  return <div className="camera-observation">
    <div className="camera-observation-lead">
      <span className="camera-eyebrow">Última inferência · {vision.status === "ready" ? "disponível" : vision.status === "warming" ? "a preparar" : "indisponível"}</span>
      <p>{vision.status === "ready" && vision.people !== null
        ? <><strong>{vision.people}</strong> pessoas neste quadro</>
        : <>Pessoas neste quadro: <b>indisponível</b></>}</p>
      <span>Não representa visitantes únicos nem identifica pessoas entre câmeras.</span>
    </div>
    <dl className="camera-facts">
      <div><dt>Modelo / revisão</dt><dd>{vision.model || "Indisponível"} / {vision.modelRevision || "Indisponível"}</dd></div>
      <div><dt>Confiança média das detecções</dt><dd>{vision.status === "ready" ? confidence : "Indisponível"}</dd></div>
      <div><dt>Latência da inferência</dt><dd>{vision.latencyMs !== null ? `${vision.latencyMs} ms` : "Indisponível"}</dd></div>
      <div><dt>Tempo no vídeo</dt><dd>{sourceTime(vision.sourceSeconds)}</dd></div>
      <div><dt>Observado em</dt><dd>{time}</dd></div>
      <div><dt>Idade na atualização</dt><dd>{age === null ? "Indisponível" : `${Math.floor(age)} s`}</dd></div>
    </dl>
    {vision.error && <p className="camera-note">Serviço de visão: {vision.error}</p>}
  </div>;
}

export function CameraWall({ snapshot, selectedZone, onSelectZone }: CameraWallProps) {
  const titleId = useId();
  const reducedMotion = useReducedMotion();
  const [motionOverride, setMotionOverride] = useState<boolean | null>(null);
  const [analysis, setAnalysis] = useState(false);
  const paused = motionOverride ?? reducedMotion;
  useEffect(() => { setMotionOverride(null); }, [reducedMotion]);
  const selected = snapshot.cameras.find((camera) => camera.zoneId === selectedZone);
  const otherZones = (Object.keys(ZONE_NAMES) as ZoneId[]).filter((zone) => zone !== selectedZone);
  const observationKey = selected
    ? `${snapshot.scenario.id}:${snapshot.scenario.activeSince}:${selected.variant}:${observationIdentity(selected.vision)}`
    : "";
  return <section className="camera-wall" aria-labelledby={titleId}>
    <header className="camera-section-heading">
      <div><span className="camera-eyebrow">01 / O olhar da loja</span><h2 id={titleId}>Cada zona, uma perspectiva.</h2>
        <p>Vídeo sintético 3D · não são câmeras de clientes reais.</p></div>
      <button className="camera-button" aria-pressed={!paused} onClick={() => setMotionOverride(!paused)}>
        {paused ? "Retomar vídeos neste dispositivo" : "Pausar vídeos neste dispositivo"}
      </button>
    </header>
    {reducedMotion && paused && <p className="camera-note">Movimento reduzido: os vídeos ficam pausados. Você pode retomá-los explicitamente.</p>}
    <div className="camera-wall-grid">
      <article className="camera-featured">
        <header className="camera-feed-heading">
          <div><span className="camera-eyebrow">Zona selecionada · {ZONE_NAMES[selectedZone]}</span>
            <h3>{selected?.name || "Câmera indisponível"}</h3>
            {selected && <span className="camera-id">{selected.id}</span>}</div>
          <button className="camera-button" aria-pressed={analysis} onClick={() => setAnalysis((value) => !value)}>
            {analysis ? "Voltar ao vídeo" : "Última análise"}
          </button>
        </header>
        <div className="camera-stage">
          {selected ? analysis
            ? <AnalysisFrame key={observationKey} camera={selected} snapshot={snapshot} />
            : <MovingFeed key={`${selected.id}:${selected.mediaUri}:${selected.variant}:${snapshot.scenario.activeSince}`}
                camera={selected} snapshot={snapshot} paused={paused} />
            : <AssetMessage title="Esta perspectiva ainda não está disponível" detail="Os dados da câmera aparecerão quando a fonte estiver pronta." />}
        </div>
        <div className="camera-feed-caption"><span>{analysis ? "Quadro estático · detecções da inferência" : "Vídeo sintético · sem caixas de detecção"}</span>
          <span>{selected ? `${selected.variant === "optimized" ? "Variante otimizada" : "Variante inicial"} · ${sourceTime(analysis ? selected.vision.sourceSeconds : selected.sourceSeconds)}` : "Fonte indisponível"}</span></div>
      </article>
      <div className="camera-secondary">
        {otherZones.map((zoneId) => {
          const camera = snapshot.cameras.find((item) => item.zoneId === zoneId);
          return <article className="camera-small" key={zoneId}>
            <button className="camera-zone-select" onClick={() => onSelectZone(zoneId)} aria-label={`Selecionar zona ${ZONE_NAMES[zoneId]} e sua câmera`}>
              <span><span className="camera-eyebrow">Explorar zona</span><strong>{ZONE_NAMES[zoneId]}</strong></span>
              <span aria-hidden="true">↗</span>
            </button>
            <div className="camera-stage camera-stage-small">
              {camera
                ? <MovingFeed key={`${camera.id}:${camera.mediaUri}:${camera.variant}:${snapshot.scenario.activeSince}`}
                    camera={camera} snapshot={snapshot} paused={paused} />
                : <AssetMessage title="Fonte em preparação" detail="Sem vídeo disponível para esta zona." />}
            </div>
            <p className="camera-small-caption">{camera ? `${camera.id} · ${sourceTime(camera.sourceSeconds)}` : "Câmera indisponível"} · sintético 3D
              <span className="camera-mini-age">{camera?.vision.status === "ready" && observationAge(camera.vision.observedAt, snapshot.serverTime) !== null
                ? `Inferência há ${Math.floor(observationAge(camera.vision.observedAt, snapshot.serverTime)!)} s`
                : "Inferência indisponível"}</span></p>
          </article>;
        })}
      </div>
    </div>
    {selected && <ObservationDetails camera={selected} snapshot={snapshot} />}
  </section>;
}
