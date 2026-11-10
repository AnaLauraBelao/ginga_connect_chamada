# Capoeira Chamada

Sistema web responsivo para cadastro de alunos, professores e turmas, chamada por QR Code e compensação semanal de presenças por aulas extras.

## Recursos implementados

- Configuração inicial do primeiro administrador, sem usuário/senha.
- Acesso de professores pela leitura do QR Code do crachá (câmera do navegador) ou inserção manual do conteúdo do QR.
- Administrador pode cadastrar, editar, ativar/desativar professores; gerar e revogar crachás; autorizar professores por turma.
- Administrador e professores podem cadastrar/editar alunos, criar turmas e matricular qualquer aluno em qualquer turma.
- Turmas regulares com vários dias por semana.
- Turmas extras vinculadas a uma ou várias turmas regulares; uma turma regular pode ter várias turmas extras. Os vínculos podem ser criados ou editados depois do cadastro, tanto na tela da turma regular quanto na tela da turma extra.
- Lista de chamada da turma regular ou de aula extra.
- Leitura do QR Code do crachá do aluno diretamente na tela de chamada, com seleção automática na lista.
- Cadastro rápido de aluno na própria chamada, com matrícula automática na turma atual e opção de matrícula nas turmas regulares vinculadas quando a chamada é de aula extra.
- Presença em aula extra gera crédito em todas as datas programadas das turmas regulares vinculadas, dentro da mesma semana (segunda a domingo), desde que o aluno esteja matriculado na respectiva turma regular.
- Relatório por turma que distingue `regular`, `extra_actual` e `extra_credit`, com turma e data de origem.
- Crachá de aluno e professor em PDF, com nome, apelido, corda e QR Code.
- Exportação ZIP, disponível ao administrador, com JSON completo, CSVs separados e cópia SQLite do banco.
- Interface adaptável para desktop e celular.


## Chamada pelo crachá e cadastro rápido

Na tela de chamada, a área **Registrar presença pelo crachá** aparece antes da lista de alunos. Toque em **Escanear crachá** e permita o acesso à câmera; o leitor permanece ativo para ler vários crachás em sequência. Cada leitura válida seleciona automaticamente o aluno na lista, mas a presença só é gravada quando o professor toca em **Salvar chamada**. Se o navegador não oferecer leitura de QR nativa, o código impresso no crachá pode ser informado manualmente. O acesso à câmera exige HTTPS ou `localhost`.

O botão **Cadastro rápido** abre um formulário sem sair da chamada. O novo aluno é cadastrado e matriculado automaticamente na turma atual, ficando selecionado para a chamada. Em uma aula extra, também é possível marcar uma ou mais turmas regulares vinculadas para matricular o aluno nelas; somente essas turmas escolhidas receberão os créditos de presença semanal. O cadastro rápido também disponibiliza o PDF do crachá do novo aluno.

## Requisitos

- Python 3.10 ou superior (usa `zoneinfo`).
- Navegador moderno. A leitura pela câmera usa `BarcodeDetector` quando disponível; Chrome/Edge recentes são recomendados. A câmera exige HTTPS ou `localhost`.

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Acesse `http://127.0.0.1:5000` no próprio computador. Por padrão, o servidor escuta em `0.0.0.0`, permitindo conexões da rede local; no primeiro acesso, cadastre o administrador e baixe o PDF do crachá gerado.

No Pop!_OS/Debian/Ubuntu, se preferir instalar dependências do sistema antes, use o gerenciador de pacotes da sua distribuição. A aplicação em si instala as bibliotecas Python pelo `pip`.

## Executar na rede local

O servidor já escuta em `0.0.0.0` por padrão. Para explicitar ou alterar essa configuração, use:

```bash
CAPOEIRA_HOST=0.0.0.0 python app.py
```

Para acessar pelo celular na mesma rede, substitua o endereço no navegador pelo IP local do computador, por exemplo `http://192.168.0.10:5000`. Para usar a câmera em outro dispositivo, publique o sistema por HTTPS (por exemplo, atrás de Nginx com certificado TLS). Navegadores normalmente bloqueiam câmera em HTTP fora de `localhost`.

Para produção com HTTPS, ative `CAPOEIRA_COOKIE_SECURE=1` e use Waitress atrás de um proxy TLS, como Nginx. Para produção, use Waitress:

```bash
waitress-serve --listen=0.0.0.0:5000 app:app
```

## Configurações por variável de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `CAPOEIRA_HOST` | `0.0.0.0` | Interface de rede do servidor Flask de desenvolvimento |
| `CAPOEIRA_PORT` | `5000` | Porta |
| `CAPOEIRA_DEBUG` | `0` | Ativa debug somente se `1` |
| `CAPOEIRA_COOKIE_SECURE` | `0` | Use `1` quando o site estiver publicado com HTTPS |
| `CAPOEIRA_DB_PATH` | `instance/capoeira.sqlite3` | Caminho do banco SQLite |

A chave de sessão é criada automaticamente em `instance/flask_secret`. Preserve esse arquivo entre reinicializações e mantenha-o protegido. Ela também protege a credencial QR dos professores; ao migrar o sistema para outro computador, copie esse arquivo de forma segura junto com o banco. Não compartilhe esse arquivo.

## Como funciona a compensação da aula extra

Exemplo: turma regular A ocorre terça e quinta; aula extra X ocorre sábado e está vinculada à turma A. Se um aluno matriculado em A for marcado como presente em X no sábado, o sistema registra:

1. `extra_actual` na própria turma X, na data de sábado.
2. `extra_credit` na turma A, na terça e na quinta daquela mesma semana.

O mesmo funcionamento vale para várias turmas vinculadas. Quando uma turma extra está ligada a duas turmas regulares, o aluno só recebe crédito nas turmas em que estiver matriculado. Presença regular tem prioridade sobre crédito extra para a mesma data, evitando contagem duplicada. A semana é considerada de segunda-feira a domingo.

O lançamento da presença na aula extra pode ser feito mesmo depois das aulas regulares da semana. O histórico é recalculado a partir das chamadas salvas para preservar a origem e evitar duplicidade.

## Estrutura da exportação

O botão **Exportar dados** gera um ZIP contendo:

- `dados.json`: cadastros, horários, permissões, matrículas, vínculos e histórico de presença.
- `turmas.csv`, `professores.csv`, `alunos.csv`.
- `horarios_turmas.csv`, `professores_por_turma.csv`, `alunos_por_turma.csv`.
- `vinculos_turmas_extras.csv`, `sessoes_chamada.csv`.
- `presencas_por_turma.csv`: uma linha por presença efetiva, com turma/data de destino, turma/data de origem e tipo (`regular`, `extra_actual`, `extra_credit`).
- `banco.sqlite3`: cópia integral do banco no momento da exportação.

A exportação não inclui `instance/flask_secret` por segurança. Para migrar a instalação e manter os crachás de professor existentes, copie esse arquivo separadamente por um canal seguro. Se ele for perdido, inicie a aplicação para gerar uma nova chave e execute `python reemitir_crachas.py` no servidor. O script revoga os QR Codes anteriores e gera um ZIP com novos PDFs para todos os professores ativos; depois, entregue os novos crachás.

## Observações de segurança importantes

- Não há senha: o QR Code do professor funciona como uma credencial de acesso. Quem tiver uma cópia do QR poderá entrar como aquele professor. Mantenha os crachás protegidos e revogue/regenere o crachá imediatamente em caso de perda.
- O sistema deve ser publicado somente em rede confiável e, para acesso por celular à câmera, com HTTPS. Não exponha diretamente o servidor de desenvolvimento à internet.
- O primeiro administrador é criado somente quando o banco ainda não possui professores.
- A exportação completa é restrita a administradores. Ela inclui dados pessoais dos alunos e professores; armazene o arquivo com cuidado.

## Limitações conhecidas desta primeira versão

- A presença é lançada por data, não há ainda gestão de calendário de feriados ou cancelamentos.
- A compensação considera todos os dias da semana (segunda a domingo) em que a turma regular tem horário cadastrado; não exige que a chamada regular já tenha sido aberta.
- Não há sincronização em nuvem ou modo offline. O SQLite fica no servidor que hospeda a aplicação.

## Consulta de chamadas e relatórios de frequência (versão 0.6)

A opção **Chamadas** na navegação abre a lista das turmas que o professor pode consultar. Cada turma possui uma tela de relatório com:

- Atalho **Mensal**: do primeiro dia do mês atual até hoje.
- Atalho **Anual**: de 1º de janeiro até hoje.
- Período personalizado: datas inicial e final escolhidas pelo usuário.
- Resumo por aluno com total de presenças, percentual e separação entre presença regular e compensação por aula extra na tela; o texto copiado permanece compacto, com apenas nome, fração e percentual.
- Histórico detalhado com data considerada, aluno, tipo de presença, turma/data de origem e professor que registrou.
- Botão **Copiar relatório**, que coloca na área de transferência um texto pronto para compartilhar. O campo de texto também fica disponível para seleção e cópia manual.

O texto copiado segue este formato:

```text
**RELATÓRIO DE FREQUÊNCIA MENSAL**

Turma: Nome da turma
Total de aulas no período: 8 (+1 extra)

**Frequência por aluno:**
- Nome do aluno: 7/8 (87,5%)
```

No atalho mensal/anual, o intervalo termina na data atual para não contabilizar aulas futuras. O total de aulas regulares parte dos dias semanais configurados para a turma, dentro do período e a partir da criação da turma, mas nunca fica abaixo da quantidade de chamadas efetivamente salvas no período. Assim, turmas antigas ou com horários semanais ausentes/incompletos não exibem zero quando já existem chamadas registradas. A tela também informa separadamente quantas chamadas foram salvas. As aulas extras exibidas no total são sessões de chamada salvas nas turmas extras vinculadas durante o período. A frequência considera presenças efetivas por data da turma consultada; em turmas regulares, as compensações por aulas extras contam como presença na data regular correspondente.


## Vínculos editáveis e relatório combinado (versão 0.6)

Os vínculos entre turmas não precisam mais ser definidos durante a criação. Depois de criar a turma, abra seus detalhes:

- Na turma extra, marque as turmas regulares que ela atende.
- Na turma regular, marque as aulas extras que podem compensar suas aulas.
- Salve os vínculos; o histórico de presenças é reconstruído para adicionar ou remover compensações afetadas.
- Um aluno presente na turma extra só recebe créditos em uma turma regular se estiver matriculado nela. Um aluno matriculado diretamente na turma extra, mas sem matrícula em nenhuma das turmas regulares atendidas por ela, permanece no grupo de frequência da aula extra.

Na tela **Chamadas**, a seção **Relatório de várias turmas** permite selecionar mais de uma turma e escolher o período. No relatório combinado:

- Cada aluno matriculado em uma ou mais turmas regulares selecionadas considera no denominador somente as aulas dessas turmas em que está matriculado.
- Presenças de aulas extras selecionadas contam como compensação apenas quando a aula extra está vinculada à turma regular do aluno. As sessões extras não aumentam o denominador dos alunos das turmas regulares.
- Alunos matriculados diretamente em uma aula extra, mas sem matrícula em nenhuma das turmas regulares atendidas por aquela aula extra, são listados em uma seção separada. Para eles, entram no denominador apenas as chamadas extras em que estão matriculados. Alunos de turmas regulares atendidas pela aula extra que não foram selecionadas no relatório não são reclassificados como alunos exclusivos da aula extra.
- O botão **Copiar relatório** copia o texto combinado para a área de transferência.

O IP de escuta padrão do servidor foi alterado para `0.0.0.0`. Isso permite acesso a partir de outros dispositivos na rede local, mas não significa que seja seguro expor o servidor de desenvolvimento diretamente à internet. Use HTTPS e um servidor WSGI atrás de proxy reverso para produção.
