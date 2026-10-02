import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const output = join(dirname(fileURLToPath(import.meta.url)), "..", "docs", "images");
mkdirSync(output, { recursive: true });
const colors = {
  blue: ["#1864ab", "#dbeafe"],
  green: ["#2f9e44", "#dcfce7"],
  orange: ["#e67700", "#fff3bf"],
  gray: ["#495057", "#f1f3f5"],
};
const escape = text => text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

function diagram(name, width, height, title, subtitle) {
  const elements = [];
  const svg = [];
  let serial = 0;
  const common = (type, x, y, w, h, stroke = "#000000") => ({
    id: `${name}-${++serial}`, type, x, y, width: w, height: h, angle: 0,
    strokeColor: stroke, backgroundColor: "transparent", fillStyle: "solid",
    strokeWidth: 1.5, strokeStyle: "solid", roughness: 0, opacity: 100,
    groupIds: [], frameId: null, roundness: null, seed: serial * 317,
    version: 1, versionNonce: serial * 997, isDeleted: false,
    boundElements: null, updated: 1, link: null, locked: false,
  });
  const text = (x, y, content, size = 17, bold = false) => {
    const lines = content.split("\n");
    const boxWidth = Math.max(...lines.map(line => line.length)) * size * 0.63;
    const lineHeight = size * 2.5;
    const element = {
      ...common("text", x, y, boxWidth, lineHeight * lines.length),
      text: content, originalText: content, fontSize: size, fontFamily: 2,
      textAlign: "left", verticalAlign: "top", containerId: null,
      autoResize: false, lineHeight: 2.5,
    };
    elements.push(element);
    svg.push(`<text x="${x}" y="${y + size}" font-size="${size}" font-weight="${bold ? 650 : 400}" fill="#000000">${lines.map((line, i) => `<tspan x="${x}" dy="${i ? lineHeight : 0}">${escape(line)}</tspan>`).join("")}</text>`);
  };
  const rect = (x, y, w, h, color, container = false) => {
    const [stroke, fill] = colors[color];
    elements.push({
      ...common("rectangle", x, y, w, h, stroke),
      backgroundColor: container ? "transparent" : fill,
      roundness: { type: 3 }, strokeStyle: container ? "dashed" : "solid",
    });
    svg.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" fill="${container ? "none" : fill}" stroke="${stroke}" stroke-width="1.5"${container ? ' stroke-dasharray="7 5"' : ""}/>`);
  };
  const box = (x, y, w, h, heading, body, color = "gray") => {
    rect(x, y, w, h, color);
    text(x + 18, y + 15, heading, 20, true);
    text(x + 18, y + 53, body, 16);
  };
  const arrow = (points, color = "gray", dashed = false) => {
    const [x, y] = points[0];
    const relative = points.map(([px, py]) => [px - x, py - y]);
    elements.push({
      ...common("arrow", x, y, Math.max(...relative.map(p => p[0])) - Math.min(...relative.map(p => p[0])),
        Math.max(...relative.map(p => p[1])) - Math.min(...relative.map(p => p[1])), colors[color][0]),
      points: relative, startBinding: null, endBinding: null, startArrowhead: null,
      endArrowhead: "arrow", lastCommittedPoint: null, elbowed: false,
      strokeStyle: dashed ? "dashed" : "solid", strokeWidth: 2,
    });
    svg.push(`<polyline points="${points.map(point => point.join(",")).join(" ")}" fill="none" stroke="${colors[color][0]}" stroke-width="2" marker-end="url(#arrow-${color})"${dashed ? ' stroke-dasharray="6 5"' : ""}/>`);
  };
  text(35, 24, title, 28, true);
  text(35, 80, subtitle, 17);
  return {
    text, rect, box, arrow,
    save() {
      writeFileSync(join(output, `${name}.excalidraw`), JSON.stringify({
        type: "excalidraw", version: 2, source: "VITRINE pattern",
        elements, appState: { viewBackgroundColor: "#ffffff", gridSize: null }, files: {},
      }, null, 2) + "\n");
      const defs = Object.entries(colors).map(([key, [stroke]]) =>
        `<marker id="arrow-${key}" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto"><path d="M1 1 L9 5 L1 9" fill="none" stroke="${stroke}" stroke-width="1.5"/></marker>`).join("");
      writeFileSync(join(output, `${name}.svg`),
        `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="title desc"><title id="title">${escape(title)}</title><desc id="desc">${escape(subtitle)}</desc><defs>${defs}</defs><rect width="${width}" height="${height}" fill="#ffffff"/><g font-family="Segoe UI,Arial,sans-serif">${svg.join("\n")}</g></svg>\n`);
    },
  };
}

const architecture = diagram("architecture", 1540, 1110,
  "VITRINE — arquitetura do ciclo de decisão",
  "Vídeos e PDV sintéticos; inferência e agentes reais. Execução inteiramente na nuvem, sem componente na loja.");
architecture.box(465, 145, 610, 140, "Workspace React + pessoa responsável",
  "Streams, evidências, PDV e comparação de períodos\nAprovação explícita antes de alterar a simulação", "blue");
architecture.rect(30, 340, 720, 405, "blue", true);
architecture.text(50, 354, "AZURE CONTAINER APPS / VNET PRÓPRIA", 17, true);
architecture.box(55, 410, 330, 285, "API / FastAPI",
  "Sessão + mídia autorizada\nRelógio e PDV simulados\nContexto e auditoria\nValida e aplica aprovação", "blue");
architecture.box(415, 410, 310, 285, "Worker de visão / CPU",
  "FFmpeg → JPEG amostrado\nRT-DETRv2 → pessoas/caixas\nGPT-5.4 → leitura visual\nSomente enquanto ativo", "green");
architecture.rect(800, 340, 710, 405, "green", true);
architecture.text(820, 354, "MICROSOFT FOUNDRY / CONTA E PROJETO", 17, true);
architecture.box(825, 410, 310, 285, "Prompt agents gerenciados",
  "Merchandising + avaliação\nGPT-4.1 / versões fixadas\nread_demo_context\nPropõem; não executam", "green");
architecture.box(1165, 410, 320, 285, "Deployment multimodal",
  "GPT-5.4 / retail-vision\nRecebe pixels do frame\nDescreve a exposição\nNão mede vendas ou fluxo", "green");
architecture.arrow([[645, 285], [645, 315], [220, 315], [220, 410]], "blue");
architecture.text(65, 289, "HTTPS + sessão", 15);
architecture.arrow([[570, 695], [570, 720], [1305, 720], [1305, 695]], "green");
architecture.text(810, 697, "JPEG + autenticação Entra", 14);
architecture.arrow([[385, 490], [400, 490], [400, 388], [795, 388], [795, 490], [825, 490]], "green");
architecture.text(445, 318, "agent_reference / ferramenta somente leitura", 14);
architecture.box(55, 815, 455, 170, "Cosmos DB for NoSQL",
  "Estado + relógio + observações + execuções\nETag, versão do layout e outbox de auditoria\nPersistência canônica; não é um grafo", "gray");
architecture.box(545, 815, 455, 170, "Blob Storage privado",
  "Seis MP4 originais: 3 câmeras × 2 layouts\nJPEG + ID da observação + ETag\nCaixas somente no frame correspondente", "gray");
architecture.box(1035, 815, 450, 170, "Serviços de plataforma",
  "ACR / imagens independentes\nKey Vault / senha da demonstração\nManaged Identity / DNS / Log Analytics", "gray");
architecture.arrow([[220, 695], [220, 815]], "gray");
architecture.arrow([[570, 695], [570, 775], [400, 775], [400, 815]], "gray");
architecture.arrow([[705, 695], [705, 815]], "gray");
architecture.arrow([[350, 695], [350, 758], [850, 758], [850, 815]], "gray");
architecture.text(55, 1010,
  "Private Endpoints: Blob, Cosmos, Key Vault e Foundry. ACR usa endpoint público autenticado; UI usa HTTPS autenticado.\nFoundry Basic: armazenamento de agentes gerenciado pelo serviço; Private Link de entrada não equivale a isolamento de saída BYO VNet.", 16);
architecture.save();

const loop = diagram("decision-loop", 1540, 1120,
  "Da observação à intervenção — sem confundir inferência com efeito",
  "Cada etapa preserva a origem da informação. A aprovação não transforma uma hipótese em prova de causalidade.");
loop.box(40, 150, 440, 170, "1 / Fonte visual sintética",
  "Replay de MP4s originais de uma loja 3D\nO vídeo está no Azure; não é uma câmera real\nVersão inicial ou rearranjada do cenário", "blue");
loop.box(550, 150, 440, 170, "2 / Percepção real",
  "RT-DETRv2: pessoas visíveis e caixas\nGPT-5.4: descrição da exposição\nFrame, instante, modelo e limitações", "green");
loop.box(1060, 150, 440, 170, "Fonte operacional simulada",
  "Vendas, devoluções, estoque e visitas\nRelógio determinístico + eventos do PDV\nNão são medidas inferidas pela câmera", "orange");
loop.arrow([[480, 235], [550, 235]], "blue");
loop.box(350, 420, 840, 155, "3 / Contexto autorizado e versionado",
  "Observações reais + métricas sintéticas, com proveniência distinta\nEpoch e layout impedem reutilizar evidência de outro cenário\nread_demo_context devolve um retrato limitado, imutável e somente leitura", "gray");
loop.arrow([[770, 320], [770, 420]], "green");
loop.arrow([[1280, 320], [1280, 365], [1100, 365], [1100, 420]], "orange");
loop.box(40, 665, 440, 180, "4 / Agente de merchandising",
  "Formula uma hipótese operacional\nCita evidências e ressalvas\nNão possui ferramenta para alterar a loja", "green");
loop.box(550, 665, 440, 180, "5 / Aprovação humana",
  "Confirma ação do catálogo, cenário e layout\nRegistra intervenção e a execução de origem\nTroca o layout e os parâmetros simulados", "blue");
loop.box(1060, 665, 440, 180, "6 / Agente de avaliação",
  "Lê antes / depois / controle virtual\nExplica amostra e restrições\nSem prometer ganho causal real", "green");
loop.arrow([[420, 575], [260, 575], [260, 665]], "gray");
loop.arrow([[480, 755], [550, 755]], "blue");
loop.arrow([[990, 755], [1060, 755]], "blue");
loop.arrow([[1280, 665], [1280, 610], [1120, 610], [1120, 575]], "gray", true);
loop.text(1200, 593, "novo contexto", 14);
loop.box(40, 935, 1460, 135, "Limite da demonstração",
  "O detector não mede compradores únicos, permanência ou conversão. O PDV e o controle são sintéticos.\nAs janelas de 60 segundos ilustram o mecanismo. Uma loja real exige integração, governança e desenho experimental adequado.", "orange");
loop.save();
