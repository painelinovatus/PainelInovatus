# app.py
from flask import Flask, render_template, jsonify, send_file
import traceback
import io
from datetime import datetime
import os
import threading
import time
import copy

# Import extra para PDF (corrige o erro de mm)
from reportlab.lib.units import mm  # ✅ corrigido aqui

app = Flask(__name__)

# -------------------------
# IMPORTS DO MONITOR
# -------------------------
try:
    from monitor import ensure_started, LATEST_DATA, get_status_data as monitor_get_status_data, gerar_relatorio_pdf
except Exception:
    try:
        from monitor import get_status_data as monitor_get_status_data
    except Exception:
        monitor_get_status_data = None
    ensure_started = None
    LATEST_DATA = None
    gerar_relatorio_pdf = None

# -------------------------
# CONFIGURAÇÕES
# -------------------------
DATA_REFRESH_SECONDS = 5  # intervalo de atualização no fallback
FALLBACK_MONITOR_THREAD_NAME = "fallback_monitor_thread"
LOCK = threading.Lock()

# Cache local caso o monitor não exporte LATEST_DATA
_local_latest = {}

# -------------------------
# FUNÇÕES DE MONITORAMENTO
# -------------------------
def _fallback_monitor_loop():
    """Executa atualizações periódicas caso o monitor não tenha thread própria."""
    global _local_latest
    if not callable(monitor_get_status_data):
        print("[FALLBACK MONITOR] monitor_get_status_data indisponível.")
        return

    print("[FALLBACK MONITOR] Iniciando loop de atualização.")
    while True:
        try:
            data = monitor_get_status_data()
            with LOCK:
                _local_latest = data.copy() if isinstance(data, dict) else {}
        except Exception:
            print("[FALLBACK MONITOR] Erro ao atualizar dados.")
            traceback.print_exc()
        time.sleep(DATA_REFRESH_SECONDS)


# Inicializa o monitor (ou cria fallback)
if callable(ensure_started):
    try:
        ensure_started()
        print("[INFO] monitor.ensure_started() chamado.")
    except Exception:
        print("[WARN] Falha ao chamar ensure_started() — iniciando fallback.")
        traceback.print_exc()
        if monitor_get_status_data and not any(t.name == FALLBACK_MONITOR_THREAD_NAME for t in threading.enumerate()):
            t = threading.Thread(target=_fallback_monitor_loop, name=FALLBACK_MONITOR_THREAD_NAME, daemon=True)
            t.start()
else:
    if monitor_get_status_data and not any(t.name == FALLBACK_MONITOR_THREAD_NAME for t in threading.enumerate()):
        t = threading.Thread(target=_fallback_monitor_loop, name=FALLBACK_MONITOR_THREAD_NAME, daemon=True)
        t.start()


# -------------------------
# FUNÇÃO AUXILIAR
# -------------------------
def _get_current_data():
    """Obtém snapshot atual do cache (ou executa monitor_get_status_data)."""
    try:
        if isinstance(LATEST_DATA, dict) and LATEST_DATA.get("timestamps") is not None:
            with LOCK:
                return copy.deepcopy(LATEST_DATA)
    except Exception:
        pass

    try:
        if isinstance(_local_latest, dict) and _local_latest.get("timestamps") is not None:
            with LOCK:
                return copy.deepcopy(_local_latest)
    except Exception:
        pass

    try:
        if callable(monitor_get_status_data):
            return monitor_get_status_data()
    except Exception:
        traceback.print_exc()

    return {"timestamps": [], "data": {}, "status_colors": {}, "quedas": [], "oscilacoes": []}


# -------------------------
# ROTAS DO FLASK
# -------------------------
@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception:
        traceback.print_exc()
        return "<h1>Erro interno no servidor</h1>", 500


@app.route('/graficos')
def graficos():
    try:
        return render_template('graficos.html')
    except Exception:
        traceback.print_exc()
        return "<h1>Erro ao carregar a página de gráficos</h1>", 500


@app.route('/data')
def data():
    """Endpoint que fornece os dados em tempo real para o painel."""
    try:
        dados = _get_current_data()
        return jsonify(copy.deepcopy(dados))
    except Exception:
        traceback.print_exc()
        return jsonify({
            "timestamps": [],
            "data": {},
            "status_colors": {},
            "quedas": [],
            "oscilacoes": [],
            "alerts": ["Erro ao obter dados do monitoramento"]
        }), 500


@app.route('/download-relatorio')
def download_relatorio():
    """Gera e retorna o relatório PDF."""
    try:
        dados = _get_current_data()

        # Usa a função personalizada do monitor se existir
        if callable(gerar_relatorio_pdf):
            try:
                resultado = gerar_relatorio_pdf(dados)
                if isinstance(resultado, str) and os.path.exists(resultado):
                    return send_file(resultado,
                                     as_attachment=True,
                                     download_name=f"relatorio_monitoramento_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                                     mimetype="application/pdf")
                if hasattr(resultado, "read"):
                    resultado.seek(0)
                    return send_file(resultado,
                                     as_attachment=True,
                                     download_name=f"relatorio_monitoramento_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                                     mimetype="application/pdf")
            except Exception:
                print("[WARN] gerar_relatorio_pdf falhou — usando fallback.")
                traceback.print_exc()

        # -------------------------
        # Fallback: Gera PDF simples em memória
        # -------------------------
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
        from reportlab.lib import colors

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4,
                                leftMargin=18*mm, rightMargin=18*mm, topMargin=25*mm, bottomMargin=20*mm)
        styles = getSampleStyleSheet()
        story = []

        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        # Adiciona logo se existir
        logo_path = os.path.join(app.static_folder or "static", "images", "logo_inovatus.png")
        if os.path.exists(logo_path):
            try:
                story.append(Image(logo_path, width=120, height=48))
                story.append(Spacer(1, 8))
            except Exception:
                pass

        story.append(Paragraph("<b>Relatório de Quedas e Oscilações</b>", styles["Title"]))
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"Gerado em: {agora}", styles["Normal"]))
        story.append(Spacer(1, 12))

        relatorio = []
        quedas = dados.get("quedas", []) or []
        oscilacoes = dados.get("oscilacoes", []) or []

        for q in quedas:
            tempo = q.get("tempo", 0)
            try:
                tempo_num = float(tempo) if tempo not in (None, "-", "") else 0.0
            except Exception:
                tempo_num = 0.0
            if tempo_num > 5:
                relatorio.append((q.get("data", "-"), q.get("nome", "-"), f"{tempo_num:.1f}", "Queda"))

        for o in oscilacoes:
            relatorio.append((o.get("data", "-"), o.get("nome", "-"), "-", "Oscilação"))

        if not relatorio:
            story.append(Paragraph("Nenhuma unidade apresentou quedas ou oscilações relevantes.", styles["Normal"]))
        else:
            tabela_dados = [["Data", "Unidade", "Tempo Fora do Ar (s)", "Tipo"]]
            tabela_dados.extend(relatorio)
            tabela = Table(tabela_dados, colWidths=[100, 230, 80, 80])
            tabela.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#111827")),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
            ]))
            story.append(tabela)

        doc.build(story)
        buffer.seek(0)
        return send_file(buffer,
                         as_attachment=True,
                         download_name=f"relatorio_monitoramento_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                         mimetype="application/pdf")

    except Exception:
        print("[ERRO AO GERAR RELATÓRIO PDF]")
        traceback.print_exc()
        return "<h3>Erro ao gerar relatório PDF</h3>", 500


# -------------------------
# EXECUÇÃO
# -------------------------
if __name__ == '__main__':
    try:
        print("[SERVIDOR FLASK INICIADO] porta 5000")
        if callable(ensure_started):
            ensure_started()
        app.run(debug=True, host='0.0.0.0', port=5000)
    except Exception:
        traceback.print_exc()
