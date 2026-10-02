import { useId, type KeyboardEvent } from "react";
import type { Snapshot, ZoneId } from "./types";
import { denimLayout, ZONE_NAMES } from "./media";
import "./cameras.css";

export interface FloorPlanProps {
  snapshot: Snapshot;
  selectedZone: ZoneId;
  onSelectZone: (id: ZoneId) => void;
}

export function FloorPlan({ snapshot, selectedZone, onSelectZone }: FloorPlanProps) {
  const id = useId();
  const layout = denimLayout(snapshot);
  const optimized = layout === "optimized";
  const replenished = snapshot.zones.find((zone) => zone.id === "essentials")?.stockStatus === "replenished" ||
    snapshot.interventions.some((item) => item.actionId === "replenish-essentials");
  const zoneProps = (zoneId: ZoneId) => ({
    role: "button",
    tabIndex: 0,
    "aria-label": `Selecionar ${ZONE_NAMES[zoneId]} e mostrar a câmera`,
    "aria-pressed": selectedZone === zoneId,
    className: `floor-zone${selectedZone === zoneId ? " floor-zone-selected" : ""}`,
    onClick: () => onSelectZone(zoneId),
    onKeyDown: (event: KeyboardEvent<SVGGElement>) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onSelectZone(zoneId);
      }
    },
  });
  return <section className="floor-plan" aria-labelledby={`${id}-heading`}>
    <header className="floor-heading">
      <div><span className="floor-eyebrow">02 / O espaço em contexto</span><h2 id={`${id}-heading`}>Mapa da loja</h2>
        <p>Selecione uma zona para explorar sua câmera.</p></div>
      <span className="floor-version">Layout {snapshot.scenario.layoutVersion}</span>
    </header>
    <div className="floor-composition">
      <figure className="floor-figure">
        <svg className="floor-svg" viewBox="0 0 640 430" role="group"
          aria-labelledby={`${id}-title ${id}-description`}>
          <title id={`${id}-title`}>Planta interativa da VITRINE</title>
          <desc id={`${id}-description`}>Esquema ilustrativo, sem escala ou coordenadas medidas.
            Denim à esquerda, essenciais à direita e caixa junto à entrada.
            Use Tab e Enter ou Espaço para selecionar uma zona.
            {layout === null ? "Disposição Denim indisponível." :
              `A orientação esquemática dos expositores Denim reflete a variante ${optimized ? "otimizada" : "inicial"} do cenário.`}</desc>
          <rect className="floor-shell" x="12" y="12" width="616" height="406" rx="18" />
          <path className="floor-wall" d="M 40 348 L 40 40 L 600 40 L 600 388 L 374 388 M 266 388 L 40 388 L 40 368" />
          <text className="floor-guide-label" x="320" y="65" textAnchor="middle">VITRINE / ESPAÇO DE VENDA</text>
          <g {...zoneProps("denim")}>
            <rect className="floor-zone-area" x="58" y="84" width="242" height="236" rx="14" />
            <text className="floor-zone-name" x="78" y="113">01 — Denim</text>
            {layout !== null && <g className="floor-racks" transform={optimized ? "rotate(90 179 207)" : undefined}>
              <rect x="111" y="154" width="136" height="24" rx="5" />
              <path d="M 125 158 L 125 174 M 147 158 L 147 174 M 169 158 L 169 174 M 191 158 L 191 174 M 213 158 L 213 174 M 235 158 L 235 174" />
              <rect x="111" y="224" width="136" height="24" rx="5" />
              <path d="M 125 228 L 125 244 M 147 228 L 147 244 M 169 228 L 169 244 M 191 228 L 191 244 M 213 228 L 213 244 M 235 228 L 235 244" />
            </g>}
            <text className="floor-zone-detail" x="78" y="300">{layout === null ? "Disposição indisponível" : optimized ? "Disposição otimizada*" : "Disposição inicial*"}</text>
          </g>
          <g {...zoneProps("essentials")}>
            <rect className="floor-zone-area" x="344" y="84" width="238" height="174" rx="14" />
            <text className="floor-zone-name" x="364" y="113">02 — Essenciais</text>
            <g className="floor-racks">
              <rect x="370" y="141" width="25" height="74" rx="5" />
              <rect x="526" y="141" width="25" height="74" rx="5" />
              <rect x="432" y="152" width="57" height="48" rx="8" />
              <path d="M 438 162 L 483 162 M 438 177 L 483 177 M 438 191 L 483 191" />
            </g>
            <text className="floor-zone-detail" x="364" y="240">{replenished ? "Reposição aplicada" : "Coleção de essenciais"}</text>
          </g>
          <g {...zoneProps("checkout")}>
            <rect className="floor-zone-area" x="396" y="278" width="186" height="90" rx="14" />
            <text className="floor-zone-name" x="414" y="305">03 — Caixa</text>
            <g className="floor-racks">
              <rect x="415" y="324" width="103" height="20" rx="4" />
              <rect x="530" y="319" width="24" height="29" rx="4" />
            </g>
          </g>
          <path className="floor-passage" d="M 320 365 L 320 132" />
          <path className="floor-entry" d="M 284 390 L 320 371 L 356 390" />
          <text className="floor-guide-label" x="320" y="410" textAnchor="middle">ENTRADA</text>
          <text className="floor-guide-label" x="70" y="357">* ESQUEMA</text>
        </svg>
        <figcaption>Planta sem escala: posições e orientações são ilustrativas, não coordenadas inferidas pela visão.
          A disposição Denim acompanha a variante da câmera; na sua ausência, a intervenção aplicada.</figcaption>
      </figure>
      <div className="floor-zone-list" aria-label="Indicadores simulados por zona">
        <p className="floor-metric-label">Movimento simulado <span>Não medido pelas câmeras</span></p>
        {(Object.keys(ZONE_NAMES) as ZoneId[]).map((zoneId) => {
          const zone = snapshot.zones.find((item) => item.id === zoneId);
          return <button key={zoneId} className={`floor-zone-card${selectedZone === zoneId ? " floor-zone-card-selected" : ""}`}
            onClick={() => onSelectZone(zoneId)} aria-pressed={selectedZone === zoneId}>
            <span className="floor-card-top"><strong>{zone?.name || ZONE_NAMES[zoneId]}</strong><span aria-hidden="true">↗</span></span>
            <span className="floor-card-count">{zone ? zone.visitors.toLocaleString("pt-BR") : "—"} <span>visitas · simulação</span></span>
            <span className="floor-card-detail">{zone ? `${zone.dwellSeconds.toLocaleString("pt-BR")} s de permanência · simulação` : "Indicadores indisponíveis"}</span>
          </button>;
        })}
        <p className="floor-footnote">A planta mostra o cenário, não trajetórias reais. Alterações de layout não demonstram impacto causal em vendas reais.</p>
      </div>
    </div>
  </section>;
}
