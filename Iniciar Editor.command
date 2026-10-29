#!/bin/sh
set -e
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
    printf '%s\n' 'Primeira execução: consulte o README.md para instalar as dependências.'
    printf '%s' 'Pressione Enter para fechar.'
    read -r resposta
    exit 1
fi
exec .venv/bin/python editor.py
