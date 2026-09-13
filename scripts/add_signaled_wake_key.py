#!/usr/bin/env python3
"""One-shot: insert gateway.kanban.wake.signaled into the non-English locales.

Insertion anchors on the ``crashed:`` wake key in each catalog (present in
every locale), copying its indentation. Genuine translations for translated
catalogs, English text for untranslated ones — matching each file's existing
convention (non-translated keys carry English).
"""
from pathlib import Path

locales = Path(__file__).resolve().parent.parent / "locales"

add = {
    "af": "killed by a signal (worker died); dispatcher will retry",
    "ar": "killed by a signal (worker died); dispatcher will retry",
    "de": "durch ein Signal beendet (Worker starb); Dispatcher versucht es erneut",
    "es": "muerto por una señal (el worker terminó); el dispatcher reintentará",
    "fr": "tué par un signal (worker mort) ; le dispatcher réessaiera",
    "ga": "killed by a signal (worker died); dispatcher will retry",
    "hu": "killed by a signal (worker died); dispatcher will retry",
    "it": "terminato da un segnale (worker morto); il dispatcher riproverà",
    "ja": "シグナルにより終了（ワーカーが死亡）、dispatcher が再試行します",
    "ko": "시그널로 종료됨(워커 사망); 디스패처가 재시도합니다",
    "pt": "morto por um sinal (worker morreu); o dispatcher vai tentar novamente",
    "ru": "убит сигналом (воркер погиб); диспетчер повторит попытку",
    "tr": "bir sinyalle sonlandırıldı (worker öldü); dispatcher yeniden deneyecek",
    "uk": "вбито сигналом (воркер загинув); диспетчер повторить спробу",
    "zh": "被信号杀死（worker 死亡），dispatcher 将重试",
    "zh-hant": "被訊號終結（worker 死亡），dispatcher 將重試",
}
for code, text in add.items():
    p = locales / f"{code}.yaml"
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    if any("signaled:" in ln for ln in lines):
        print(f"{code}: already present")
        continue
    out = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.strip().startswith("crashed:"):
            indent = ln[: len(ln) - len(ln.lstrip())]
            out.append(f'{indent}signaled:            "{text}"\n')
            inserted = True
    assert inserted, f"{code}: no crashed: anchor"
    p.write_text("".join(out), encoding="utf-8")
    print(f"{code}: inserted")
