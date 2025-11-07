import os
import requests
import pytz
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- ReportLab ---
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, KeepTogether
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from collections import Counter, defaultdict

# ===============================
# CONFIGURAÇÃO BÁSICA
# ===============================
CUIABA_TZ = pytz.timezone('America/Cuiaba')
MAX_HISTORY_LEN = 100  # histórico p/ gráficos
CHECK_INTERVAL = 5     # segundos entre verificações

# --- Conexões persistentes (muito mais rápido) ---
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

SITES = {
    'Alto Garças': 'https://altogarcas.celk.com.br/',
    'Alto Paraguai': 'https://altoparaguai.celk.com.br/',
    'Aripuanã': 'https://aripuana.celk.com.br/',
    'Brasnorte': 'https://brasnorte.celk.com.br/',
    'Cassilândia': 'https://cassilandia.celk.com.br/',
    'Chapada dos Guimarães': 'https://chapadadosguimaraes.celk.com.br/',
    'Confresa': 'https://confresa.celk.com.br/',
    'Mirassol D\'Oeste': 'https://mirassoldoeste.celk.com.br/',
    'Nortelândia': 'https://nortelandia.celk.com.br/',
    'Nossa Senhora do Livramento': 'https://nossasenhoradolivramento.celk.com.br/',
    'Nova Nazaré': 'https://novanazare.celk.com.br/',
    'Nova Maringá': 'https://novamaringa.celk.com.br/',
    'Planalto da Serra': 'https://planaltodaserra.celk.com.br/',
    'Santo Antônio do Leverger': 'https://santoantoniodoleverger.celk.com.br/',
    'São José do Rio Claro': 'https://saojosedorioclaro.celk.com.br/',
    'São José dos Quatro Marcos': 'https://saojosedosquatromarcos.celk.com.br/',
    'Vila Bela': 'https://vilabela.celk.com.br/',
    'Várzea Grande': 'https://varzeagrande.celk.com.br/',
    # Hospitais
    'Hospital Aparecida': 'https://hmaparecida.celk.com.br/',
    'Hospital Aripuanã': 'https://hospitalaripuana.celk.com.br/',
    'Hospital Brasnorte': 'https://hospitalbrasnorte.celk.com.br/',
    'Hospital Confresa': 'https://hospitalconfresa.celk.com.br/',
    'Hospital Campinápolis': 'https://hospitalcampinapolis.celk.com.br/',
    'Hospital Livramento': 'https://hospitalnossasenhoradolivramento.celk.com.br/',
    'Hospital Nova Xavantina': 'https://hospitalnovaxavantina.celk.com.br/',
    'Hospital Salto do Céu': 'https://hospitalsaltodoceu.celk.com.br/',
    'Hospital São José do Rio Claro': 'https://hospitalsjrioclaro.celk.com.br/',
    'Hospital Várzea Grande': 'https://hospitalvarzeagrande.celk.com.br/',
}
ORDERED_NAMES = list(SITES.keys())

# --- Estruturas de dados ---
history = {site: [] for site in SITES}
timestamps = []
offline_time = {site: 0 for site in SITES}
oscillation_detected = {site: False for site in SITES}
lock = threading.Lock()

# Cache global (para o Flask)
LATEST_DATA = {}
_thread_started = False

# ===============================
# MONITORAMENTO OTIMIZADO
# ===============================
def check_site(nome, url):
    """Checa status HTTP da unidade (com sessão persistente)."""
    try:
        r = _session.get(url, timeout=4)
        return nome, 1 if r.status_code == 200 else 0
    except Exception:
        return nome, 0


def get_status_color(name):
    data = history[name]
    if len(data) < 2:
        return 'green' if data and data[-1] == 1 else 'red'
    if data[-1] != data[-2]:
        return 'yellow'
    return 'green' if data[-1] == 1 else 'red'


def get_status_data():
    """Executa uma rodada de verificação completa."""
    current_dt = datetime.now(CUIABA_TZ)
    current_time = current_dt.strftime('%H:%M:%S')

    status_dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(check_site, n, u): n for n, u in SITES.items()}
        for fut in as_completed(futures):
            nome, status = fut.result()
            status_dict[nome] = status

    with lock:
        if len(timestamps) >= MAX_HISTORY_LEN:
            timestamps.pop(0)
        timestamps.append(current_time)

        relatorio_quedas = []
        relatorio_oscilacoes = []

        for nome, status in status_dict.items():
            if len(history[nome]) >= MAX_HISTORY_LEN:
                history[nome].pop(0)

            prev = history[nome][-1] if history[nome] else None
            history[nome].append(status)

            # offline progressivo
            if status == 0:
                offline_time[nome] += CHECK_INTERVAL
            else:
                offline_time[nome] = 0

            # oscilação detectada
            if prev is not None and prev != status:
                oscillation_detected[nome] = True
                relatorio_oscilacoes.append({
                    "data": current_time, "nome": nome, "tempo": "-", "tipo": "Oscilação"
                })
            else:
                oscillation_detected[nome] = False

            # queda >= 5s
            if offline_time[nome] >= 5:
                relatorio_quedas.append({
                    "data": current_time, "nome": nome, "tempo": offline_time[nome], "tipo": "Queda"
                })

        return {
            "timestamps": timestamps.copy(),
            "data": {n: history[n].copy() for n in ORDERED_NAMES},
            "status_colors": {n: get_status_color(n) for n in ORDERED_NAMES},
            "quedas": relatorio_quedas,
            "oscilacoes": relatorio_oscilacoes
        }


# ===============================
# LOOP DE MONITORAMENTO EM THREAD
# ===============================
def _background_loop():
    global LATEST_DATA
    while True:
        try:
            new_data = get_status_data()
            with lock:
                LATEST_DATA = new_data
        except Exception as e:
            print(f"[MONITOR LOOP ERRO] {e}")
        time.sleep(CHECK_INTERVAL)


def ensure_started():
    """Garante que o monitor de background esteja ativo."""
    global _thread_started
    if not _thread_started:
        t = threading.Thread(target=_background_loop, name="monitor_loop", daemon=True)
        t.start()
        _thread_started = True
        print("[Monitor] Thread de monitoramento iniciada.")


# ===============================
# PDF - IMPLEMENTAÇÃO
# ===============================
PRIMARY = colors.HexColor("#0A6CF0")
DARK = colors.HexColor("#1F2937")
MUTED = colors.HexColor("#6B7280")
RED = colors.HexColor("#DC2626")
AMBER = colors.HexColor("#D97706")
ROW_ALT = colors.HexColor("#F5F7FA")


def _header_footer(canvas, doc):
    """Desenha faixa superior, logo e informações no cabeçalho, e rodapé."""
    canvas.saveState()
    page_w, page_h = A4

    # faixa superior (azul)
    header_h = 25 * mm
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, page_h - header_h, page_w, header_h, fill=1, stroke=0)

    # caminho da logo (procura static/images/logo_inovatus.png relativo a este módulo)
    try:
        module_dir = os.path.dirname(os.path.abspath(__file__))
        logo_path = os.path.join(module_dir, "static", "images", "logo_inovatus.png")
        # fallback alternative path checks (caso a estrutura do projeto seja diferente)
        if not os.path.exists(logo_path):
            # tenta diretório pai
            logo_path_alt = os.path.join(module_dir, "..", "static", "images", "logo_inovatus.png")
            if os.path.exists(logo_path_alt):
                logo_path = os.path.abspath(logo_path_alt)

        # dimensões da logo (ajuste conforme necessário)
        logo_w = 36 * mm
        logo_h = 12 * mm
        logo_x = 12 * mm
        logo_y = page_h - header_h + ((header_h - logo_h) / 2)

        if os.path.exists(logo_path):
            try:
                canvas.drawImage(logo_path, logo_x, logo_y, width=logo_w, height=logo_h, mask='auto')
            except Exception as e:
                print("[PDF] Erro ao desenhar a logo:", e)
                # desenha um placeholder discreto
                canvas.setFillColor(colors.white)
                canvas.rect(logo_x, logo_y, 6 * mm, 6 * mm, fill=1, stroke=0)
        else:
            # log para debug: caminho não encontrado
            print("[PDF] Logo não encontrada em:", logo_path)
            canvas.setFillColor(colors.white)
            canvas.rect(logo_x, logo_y, 6 * mm, 6 * mm, fill=1, stroke=0)
    except Exception as ex:
        print("[PDF] Erro ao preparar logo:", ex)

    # título (na faixa azul, ao lado da logo)
    try:
        title_x = logo_x + logo_w + 6 * mm
        title_y = page_h - header_h + (header_h / 2) + 4
        canvas.setFont("Helvetica-Bold", 14)
        canvas.setFillColor(colors.white)
        canvas.drawString(title_x, title_y, "Relatório de Quedas e Oscilações")
    except Exception as e:
        print("[PDF] Erro ao desenhar título:", e)

    # data/hora no canto superior direito (dentro da faixa)
    try:
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(colors.white)
        dt_txt = datetime.now(CUIABA_TZ).strftime("Gerado em: %d/%m/%Y %H:%M:%S")
        canvas.drawRightString(page_w - 12 * mm, page_h - (header_h / 2) + 4, dt_txt)
    except Exception as e:
        print("[PDF] Erro ao desenhar data:", e)

    # rodapé: número da página
    try:
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(page_w - 12 * mm, 12 * mm, f"Página {doc.page}")
    except Exception as e:
        print("[PDF] Erro ao desenhar rodapé:", e)

    canvas.restoreState()


def gerar_relatorio_pdf(dados, arquivo_pdf="relatorio_monitoramento.pdf"):
    """Gera PDF em disco e retorna o caminho (string)."""
    # prepara dados
    quedas = (dados.get("quedas") or [])
    oscs = (dados.get("oscilacoes") or [])
    eventos = quedas + oscs

    total_eventos = len(eventos)
    total_quedas = len(quedas)
    total_oscs = len(oscs)
    unidades_afetadas = len(set(e["nome"] for e in eventos)) if eventos else 0

    cont_por_unidade = Counter(e["nome"] for e in quedas)
    tempo_por_unidade = defaultdict(int)
    for e in quedas:
        t = e.get("tempo")
        if isinstance(t, (int, float)):
            tempo_por_unidade[e["nome"]] += int(t)
    top5 = cont_por_unidade.most_common(5)

    # layout do PDF
    doc = SimpleDocTemplate(arquivo_pdf, pagesize=A4,
                            leftMargin=18*mm, rightMargin=18*mm,
                            topMargin=35*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=DARK, spaceAfter=6)
    normal = ParagraphStyle("normal", parent=styles["Normal"], fontSize=10, leading=14, textColor=DARK)
    muted = ParagraphStyle("muted", parent=styles["Normal"], fontSize=10, textColor=MUTED)
    badge_red = ParagraphStyle("badge_red", parent=styles["Normal"], alignment=1, textColor=colors.white,
                               backColor=RED, fontSize=9, leading=12, spaceBefore=2, spaceAfter=2)
    badge_amber = ParagraphStyle("badge_amber", parent=styles["Normal"], alignment=1, textColor=colors.white,
                                 backColor=AMBER, fontSize=9, leading=12, spaceBefore=2, spaceAfter=2)

    story = []

    # resumo topo
    resumo_data = [
        [Paragraph("<b>Total de eventos</b>", normal), Paragraph("<b>Quedas</b>", normal),
         Paragraph("<b>Oscilações</b>", normal), Paragraph("<b>Unidades afetadas</b>", normal)],
        [Paragraph(str(total_eventos), h2), Paragraph(str(total_quedas), h2),
         Paragraph(str(total_oscs), h2), Paragraph(str(unidades_afetadas), h2)]
    ]
    resumo_tbl = Table(resumo_data, colWidths=[doc.width/4]*4)
    resumo_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), ROW_ALT),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("BOX", (0,0), (-1,-1), 0.6, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(resumo_tbl)
    story.append(Spacer(1, 8))

    story.append(Paragraph("Top 5 unidades com mais quedas", muted))
    top_rows = [["Unidade", "Quedas", "Tempo Offline (s)"]]
    if top5:
        for nome, cnt in top5:
            top_rows.append([nome, str(cnt), str(tempo_por_unidade.get(nome, 0))])
    else:
        top_rows.append(["—", "0", "0"])
    top_tbl = Table(top_rows, colWidths=[None, 28*mm, 40*mm])
    top_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("ALIGN", (1,1), (-1,-1), "CENTER"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, ROW_ALT]),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#E5E7EB")),
    ]))
    story.append(top_tbl)
    story.append(Spacer(1, 14))

    # seção quedas
    story.append(Paragraph("Tabela de Quedas", h2))
    if not quedas:
        info_q = Table([[Paragraph("Nenhuma queda registrada no período.", normal)]], colWidths=[doc.width])
        info_q.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), ROW_ALT),
            ("BOX", (0,0), (-1,-1), 0.6, colors.HexColor("#E5E7EB")),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
            ("TOPPADDING", (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ]))
        story.append(info_q)
    else:
        linhas_q = [["Data/Hora", "Unidade", "Tempo Offline", "Tipo"]]
        for e in sorted(quedas, key=lambda x: (x["data"], x["nome"])):
            tempo_fmt = f"{int(e['tempo'])} s" if isinstance(e.get("tempo"), (int, float)) else "-"
            linhas_q.append([e["data"], e["nome"], tempo_fmt, Paragraph("Queda", badge_red)])
        tbl_q = Table(linhas_q, colWidths=[30*mm, None, 30*mm, 25*mm], repeatRows=1)
        tbl_q.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#111827")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("ALIGN", (0,0), (-1,0), "CENTER"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, ROW_ALT]),
            ("ALIGN", (0,1), (0,-1), "CENTER"),
            ("ALIGN", (-2,1), (-2,-1), "CENTER"),
            ("ALIGN", (-1,1), (-1,-1), "CENTER"),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING", (0,1), (-1,-1), 6),
            ("BOTTOMPADDING", (0,1), (-1,-1), 6),
            ("LINEBELOW", (0,0), (-1,0), 0.6, colors.HexColor("#1F2937")),
            ("GRID", (0,1), (-1,-1), 0.25, colors.HexColor("#E5E7EB")),
        ]))
        story.append(KeepTogether(tbl_q))

    story.append(Spacer(1, 16))

    # seção oscilações
    story.append(Paragraph("Tabela de Oscilações", h2))
    if not oscs:
        info_o = Table([[Paragraph("Nenhuma oscilação registrada no período.", normal)]], colWidths=[doc.width])
        info_o.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), ROW_ALT),
            ("BOX", (0,0), (-1,-1), 0.6, colors.HexColor("#E5E7EB")),
            ("LEFTPADDING", (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
            ("TOPPADDING", (0,0), (-1,-1), 8),
            ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ]))
        story.append(info_o)
    else:
        linhas_o = [["Data/Hora", "Unidade", "Tempo Offline", "Tipo"]]
        for e in sorted(oscs, key=lambda x: (x["data"], x["nome"])):
            tempo_fmt = f"{int(e['tempo'])} s" if isinstance(e.get("tempo"), (int, float)) else "-"
            linhas_o.append([e["data"], e["nome"], tempo_fmt, Paragraph("Oscilação", badge_amber)])
        tbl_o = Table(linhas_o, colWidths=[30*mm, None, 30*mm, 30*mm], repeatRows=1)
        tbl_o.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#111827")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("ALIGN", (0,0), (-1,0), "CENTER"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, ROW_ALT]),
            ("ALIGN", (0,1), (0,-1), "CENTER"),
            ("ALIGN", (-2,1), (-2,-1), "CENTER"),
            ("ALIGN", (-1,1), (-1,-1), "CENTER"),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING", (0,1), (-1,-1), 6),
            ("BOTTOMPADDING", (0,1), (-1,-1), 6),
            ("LINEBELOW", (0,0), (-1,0), 0.6, colors.HexColor("#1F2937")),
            ("GRID", (0,1), (-1,-1), 0.25, colors.HexColor("#E5E7EB")),
        ]))
        story.append(KeepTogether(tbl_o))

    # build
    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    return arquivo_pdf


__all__ = ["get_status_data", "gerar_relatorio_pdf", "LATEST_DATA", "ensure_started"]
