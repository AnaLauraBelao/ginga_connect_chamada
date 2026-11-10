"""Reemite os crachás QR de todos os professores ativos.

Use em um terminal no servidor, especialmente se a chave de sessão tiver sido perdida.
A operação revoga todos os QR Codes anteriores.
"""
from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from app import create_badge_pdf, db_connect, teacher_token_for


def main() -> None:
    with db_connect() as db:
        teachers = db.execute(
            "SELECT id,name FROM teachers WHERE active=1 ORDER BY name"
        ).fetchall()

    if not teachers:
        print("Nenhum professor ativo encontrado.")
        return

    print(f"Serão reemitidos {len(teachers)} crachá(s). Todos os QR Codes anteriores serão revogados.")
    confirmation = input("Digite REEMITIR para continuar: ").strip()
    if confirmation != "REEMITIR":
        print("Operação cancelada.")
        return

    with db_connect() as db:
        for item in teachers:
            public_id = str(uuid.uuid4())
            token = teacher_token_for(public_id)
            db.execute(
                "UPDATE teachers SET public_id=?,qr_token_hash=? WHERE id=?",
                (public_id, hashlib.sha256(token.encode("utf-8")).hexdigest(), item["id"]),
            )
        updated = db.execute(
            "SELECT * FROM teachers WHERE active=1 ORDER BY name"
        ).fetchall()

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for teacher in updated:
            pdf = create_badge_pdf("teacher", teacher)
            safe_name = "".join(ch for ch in teacher["name"] if ch.isalnum() or ch in "-_ ").strip().replace(" ", "-")
            archive.writestr(f"cracha-{teacher['id']}-{safe_name or 'professor'}.pdf", pdf.getvalue())

    instance = Path(__file__).resolve().parent / "instance"
    instance.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = instance / f"crachas-reemitidos-{stamp}.zip"
    destination.write_bytes(output.getvalue())
    print(f"Crachás gerados: {destination}")
    print("Entregue o novo PDF a cada professor. Os QR Codes antigos não funcionam mais.")


if __name__ == "__main__":
    main()
