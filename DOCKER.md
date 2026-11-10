# Docker — Capoeira Chamada

## Preparar e iniciar

```bash
cp .env.example .env
sed -i "s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
mkdir -p data
docker compose up -d --build
```

Acesse `http://IP_DO_SERVIDOR:5000` (ou a porta definida em `CAPOEIRA_PORT`).

## Comandos úteis

```bash
# Acompanhar logs
docker compose logs -f

# Parar sem apagar os dados
docker compose down

# Iniciar novamente
docker compose up -d

# Atualizar/reconstruir a imagem após alterações no código
docker compose up -d --build

# Abrir um shell no container
docker compose exec capoeira-chamada sh
```

## Persistência

O banco SQLite e a chave de sessão são armazenados em `./data`, montado em `/app/instance` dentro do container. Faça backup de toda essa pasta. Não apague `data/flask_secret` nem `data/capoeira.sqlite3` ao atualizar o projeto.

## QR Code pela câmera do celular

O acesso por HTTP funciona na rede local. Porém, navegadores móveis normalmente exigem HTTPS para liberar a câmera fora de `localhost`. Para leitura dos crachás pela câmera em outros dispositivos, coloque o serviço atrás de um proxy HTTPS (por exemplo, Caddy ou Nginx) e configure `CAPOEIRA_COOKIE_SECURE=1`.
