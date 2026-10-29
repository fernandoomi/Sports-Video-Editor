# Editor Visual · 3.9.0

Projeto open source sob a licença MIT. Consulte [LICENSE](LICENSE) para os termos.

## Abas e presets

A interface está organizada nas abas **01 Entrada**, **02 Edição** e **03 Exportação**. Na aba Exportação, é possível salvar até cinco presets. Eles ficam em `~/Documents/Sports Video Editor Presets.xml` e podem ser transferidos entre computadores.

## Marca-d’água

O texto da marca-d’água é editável. Em **Avançado**, ajuste tamanho, transparência, ponto de ancoragem, deslocamentos horizontal e vertical, fonte, negrito, itálico, sublinhado, sombra e uma das oito cores. As opções são aplicadas ao filtro de texto do FFmpeg.

Aplicativo Python/Qt para macOS, com velocidade automática baseada no script `00 Editor s corte - 00 Final.py` fornecido em 28/10/2026. Organizado em entrada, edição, saída e logs. Não depende de servidor ou navegador.

Janela ampla de 1340 × 740, com todas as seções visíveis sem rolagem da janela. A área de arrastar arquivos fica à esquerda, com a lista abaixo, e as opções de edição à direita; as duas barras de progresso têm 4 pixels de altura e ficam agrupadas. Apenas as listas de arquivos e logs têm rolagem interna quando necessário.

## Instalar e abrir

Requer macOS, Python 3.10 ou superior e FFmpeg com VideoToolbox. No Terminal, dentro desta pasta:

```sh
brew install ffmpeg
/usr/local/bin/python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python editor.py
```

Se seu Python estiver em outro local, substitua `/usr/local/bin/python3` pelo caminho instalado (`python3`, por exemplo). O motor procura FFmpeg e FFprobe em `/opt/homebrew/bin`, `/usr/local/bin` e no PATH.

Depois da instalação, dê dois cliques em **Iniciar Editor.command**. Também é possível executar `.venv/bin/python editor.py` no Terminal. O arquivo de inicialização deve ter permissão de execução: `chmod +x "Iniciar Editor.command"`.

## Usar

1. Arraste vídeos para a área de entrada ou clique em **Buscar arquivos**. O seletor mostra os formatos de vídeo suportados; arquivos de outros tipos são ignorados também ao arrastar. A lista evita duplicatas e permite remoção antes do processamento.
   Para buscar em uma árvore de pastas, clique em **Buscar vídeos em pasta e subpastas…** e escolha a pasta inicial. Os vídeos encontrados, inclusive nas subpastas, são adicionados à fila sem duplicatas. A busca inclui também pastas de saída que estejam dentro da pasta escolhida.
2. Marque **Fade in** para habilitar a duração de 0,1 a 1 segundo, ajustável em décimos. O quadro zero fica visível para a capa; o fade começa do preto no quadro seguinte. O programa não controla qual quadro cada aplicativo externo escolhe como miniatura.
3. Para adicionar música, marque a opção e selecione a trilha. Como no original, o áudio da câmera é removido. A música começa com atraso de 0,3 s e termina junto com o vídeo. Se for curta, o restante fica em silêncio, sem repetição.
4. Escolha velocidade automática ou uma velocidade fixa relativa ao arquivo de entrada. **2×** reduz a duração pela metade; **0,5×** dobra a duração. Sem rampa, o modo manual aplica essa velocidade ao arquivo todo. Com rampa, ela define a velocidade de destino da transição.
5. Opcionalmente limite a duração, em segundos (vírgula ou ponto decimal). **Manter início** corta o final; **manter final** corta o início. O corte é aplicado à timeline após a velocidade e antes do fade. Um limite maior que o vídeo não estende o arquivo. Há arredondamento de até um quadro na saída de 24 fps.
6. Escolha a pasta e clique em **Iniciar edição**. A primeira barra acompanha o lote; a segunda mostra o vídeo que enviou a atualização mais recente. Com processamento paralelo, o nome identifica esse vídeo. O progresso geral representa a média dos trabalhos, incluindo os encerrados com falha; consulte o resumo e os logs para confirmar o resultado.

Os botões aparecem na ordem **▶️ Iniciar edição**, **⏸️ Pausar** e **❌ Cancelar**. **Pausar** suspende os processos do FFmpeg/FFprobe e bloqueia novas etapas em todos os vídeos do lote; o botão muda para **Retomar** para continuar do mesmo ponto. É possível cancelar mesmo durante a pausa.

O botão **Cancelar** interrompe as exportações em andamento e limpa arquivos temporários. Resultados já concluídos permanecem na pasta. Para fechar durante um lote, aguarde o cancelamento e feche novamente.

Os tempos de fade in, trecho rápido, rampa, slow e fades de saída podem ser ajustados com sliders ou digitados nos campos numéricos ao lado. Os controles usam décimos de segundo. Os sliders de tempos longos expandem a escala quando um valor maior é digitado.

## Últimas escolhas

O editor salva automaticamente as opções de edição, a pasta de saída, a trilha selecionada, a fluidez, os controles de rampa, o número de vídeos em paralelo e a ação ao terminar. Essas escolhas são restauradas na próxima abertura, junto com o tamanho/posição da janela e a última pasta usada para buscar vídeos.

As preferências são gravadas localmente pelo sistema de configurações do macOS (QSettings, organização `BemCorrido`, aplicativo `EditorVisual`), sem alterar os arquivos de vídeo. A fila e o andamento de um lote não são restaurados. Decisões de sobrescrever/pular continuam sendo solicitadas a cada lote, mesmo que tenha marcado aplicar a todos no lote anterior.

Se a trilha salva não existir mais, o seletor indica **não encontrada** e o editor pede um arquivo válido ao iniciar. Preferências ausentes ou inválidas usam os padrões do programa. Os testes utilizam configurações temporárias isoladas para preservar as escolhas reais do usuário.

## Fluidez adaptativa

O seletor **Fluidez** oferece:

- **Adaptativa · até 60 fps** (padrão).
- **Adaptativa · até 120 fps**.
- **240 fps · experimental**.
- **Fixa · 24 fps**, para reproduzir a cadência anterior.

No modo adaptativo, os trechos constantes em slow ficam a 24 fps. As rampas e os trechos normais/acelerados usam uma taxa maior, limitada pela taxa máxima disponível no trecho e pela opção escolhida. Uma fonte de 30 fps a velocidade normal, por exemplo, permanece em 30 fps, mesmo com limite de 60. A taxa é constante dentro de cada trecho e o arquivo combina essas taxas como vídeo VFR.

A exportação preserva os timestamps entre os trechos e não aplica mais a conversão global para 24 fps. Não há interpolação de movimento: nas partes da rampa em que a origem não fornece quadros suficientes para a cadência do trecho, quadros podem ser repetidos. Isso não acrescenta detalhes de movimento, mas mantém a cadência regular.

Os modos de 120 e 240 fps exigem mais do codificador e do reprodutor. O bitrate permanece em 12 Mb/s; mais quadros dividem esse mesmo orçamento de compressão. A prévia continua exibindo o original. Os logs mostram as taxas previstas para os trechos exportados.

## Início e conclusão

Ao clicar em **▶️ Iniciar edição**, o editor valida as opções e consulta os conflitos no destino. Depois exibe **Iniciando...** por 2 segundos antes de processar, sem travar a janela. Você pode cancelar durante essa espera.

Na seção **03 Saída**, escolha **Ao terminar → Abrir a pasta destino** ou **Abrir um pop-up** (padrão). O pop-up informa quantos vídeos foram concluídos, pulados e falharam. A ação é executada depois de encerrar o lote e liberar os controles, inclusive quando todos os arquivos foram pulados. Lotes cancelados não disparam a ação de conclusão.

## Fades de saída

Marque **Fade out do vídeo** para escurecer o fim do vídeo, preservando o primeiro quadro para a capa. Marque **Fade out da trilha** para reduzir o volume no fim do trecho audível. Digite a duração de cada fade em segundos, com precisão de décimos; o padrão é 1,0 s. O controle de trilha o controle fica disponível quando a trilha está ativada. Se a música terminar antes do vídeo, o fade é aplicado ao fim da própria música. Para trechos menores que a duração escolhida, o fade se ajusta à duração disponível. As escolhas são salvas automaticamente.

## Rampa de velocidade

**Tempo rápido** determina quantos segundos, medidos no vídeo final, o trecho permanece na velocidade rápida antes de cada rampa. O controle varia de 0 a 10 s; o padrão é 0,0 s, que inicia a rampa imediatamente. Com repetições, esse tempo é aplicado antes de cada bloco. **Tempo do slow** varia de 1 a 20 s. É possível selecionar 0,75× na velocidade lenta e 0,8× na rápida.

Marque **Rampa de velocidade** para fazer uma transição suave da velocidade rápida para a velocidade lenta. Os valores manuais da velocidade lenta são relativos ao arquivo; a velocidade rápida é relativa ao movimento real e, em S&Q, considera o fator de captura informado. Os dois seletores são independentes: a velocidade lenta automática usa o FPS; a rápida automática usa 1× de movimento real. A rápida fica disponível ao ativar a rampa. Escolha a duração de cada transição de **0,2 a 5 s**, em passos de 0,1 s. Os tempos são medidos na saída, após a alteração de velocidade.

- **1× (padrão):** uma rampa de entrada e slow até o final. O campo **Tempo do slow** fica desabilitado.
- **2× a 4×:** cada bloco faz rampa de entrada, mantém o slow pelo tempo escolhido e retorna suavemente à velocidade normal. O trecho restante, após todos os blocos, continua na velocidade rápida escolhida. A duração selecionada vale para cada transição de entrada e saída.
- Os blocos percorrem o vídeo em sequência, sem repetir os mesmos quadros. Se faltar material, o último bloco termina junto com o vídeo.
- No automático, vídeos próximos de 24 fps são tratados como Sony S&Q já desacelerado. Escolha o fator de captura correspondente; desmarque Sony S&Q para vídeos comuns em 24 fps. Para vídeos HFR, o slow usa a taxa de quadros detectada.
- O corte por duração continua aplicado depois do planejamento de velocidade. A prévia mostra o original, sem simular rampas ou outros efeitos.

As transições alteram os timestamps continuamente, sem interpolação de quadros. Exportação por VideoToolbox, trilha na segunda etapa e processamento paralelo permanecem disponíveis.

## Pré-visualização

Selecione um vídeo na **Fila de edição** para carregá-lo no painel de pré-visualização, à direita das opções. Use **▶️ Reproduzir** e **⏸️ Pausar prévia** para conferir o arquivo original, sem áudio. Os efeitos e cortes são aplicados na exportação. A prévia é interrompida automaticamente ao iniciar o lote, liberando o decodificador antes da exportação.

## Comportamento herdado

- Exportação HEVC ou H.264 por hardware Apple VideoToolbox, pixel format YUV420P, MP4 com faststart e slow em 24 fps e opção de fluidez adaptativa por trecho. HEVC usa identificação `hvc1` para compatibilidade Apple.
- Até dois vídeos em paralelo por padrão, configurável de um a quatro. Sem interpolação de quadros. Música adicionada em uma segunda etapa, copiando o vídeo sem recodificar e usando sua duração real.
- Detecção robusta pela maior taxa entre `r_frame_rate` e `avg_frame_rate`. O modo automático usa a mesma lógica do novo script: para FPS acima de 24, multiplica o tempo por FPS/24, arredondando taxas próximas de padrões (239,76 → 240, por exemplo). Até 24 fps, preserva a duração. Exemplo: 2 segundos a 240 fps resultam em cerca de 20 segundos, sem rampa.
- O antigo perfil fixo de fases foi substituído. A hipótese Sony S&Q fica **marcada por padrão** para vídeos próximos de 24 fps. O editor preserva o slow já gravado e usa o fator escolhido para restaurar movimento normal nas rampas; desmarque para arquivos comuns de 24 fps. Como metadados de 24 fps não identificam de forma confiável S&Q, confirme o fator de captura no seletor.
- Marca-d’água `@bemcorrido` habilitada por padrão, podendo ser desmarcada. Requer FFmpeg com filtro `drawtext`.
- Nomes sem espaços, hífens ou underscores, em maiúsculas, com sufixo `ED01`. Colisões recebem `_2`, `_3` etc. Antes de iniciar, o editor verifica os nomes finais na pasta destino e pergunta se deseja **Sobrescrever** ou **Pular**, com **Aplicar a todos os conflitos deste lote**. A opção **Cancelar lote** interrompe a preparação. A sobrescrita só substitui o arquivo antigo após a nova exportação terminar com sucesso. Os sufixos `_2`, `_3` distinguem entradas da própria fila que geram o mesmo nome; os nomes são estáveis ao repetir o lote.

A versão visual define explicitamente bitrate de vídeo de 12 Mb/s: o script original deixava a escolha para o padrão do codificador. O tamanho final depende da duração, conteúdo e codificador.

## Verificação

```sh
.venv/bin/python -m unittest -v test_engine test_ui
QT_QPA_PLATFORM=offscreen .venv/bin/python -c 'from PySide6.QtWidgets import QApplication; from editor import Window; a=QApplication([]); w=Window(); assert not w.fade_time.isEnabled(); w.fade.setChecked(True); assert w.fade_time.isEnabled(); w.close()'
```

Os testes incluem o perfil de velocidade, cortes em ambas as direções, nomes em colisão, cancelamento e renderizações reais para conferir capa/fade, duração e atraso da trilha. Os testes de renderização exigem FFmpeg e VideoToolbox no Mac.

Arquitetura: [interface](editor.py), [motor](engine.py), [testes](test_engine.py) e [dependências](requirements.txt).

## Histórico

**3.9.0 · 28/10/2026:** sliders com campos numéricos para tempos de fade e velocidade, com ajuste em décimos e compatibilidade com preferências anteriores.

**3.8.0 · 28/10/2026:** controle do tempo rápido antes de cada rampa e opções de velocidade lenta 0,75× e rápida 0,8×.

**3.7.0 · 28/10/2026:** controles independentes para velocidade rápida e lenta nas rampas, com opção automática e preservação nas preferências.

**3.6.0 · 28/10/2026:** fades de saída com durações independentes em décimos de segundo, posicionados abaixo de vídeos em paralelo.

**3.5.0 · 28/10/2026:** opções independentes de fade out de vídeo e da trilha, com duração de 1 segundo e ajuste para trechos curtos.

**3.4.0 · 28/10/2026:** detecção automática de HFR e hipótese Sony S&Q para vídeos próximos de 24 fps habilitada por padrão, inclusive nas rampas; migração das preferências existentes.

**3.3.0 · 28/10/2026:** salvamento automático das últimas escolhas, restauração da trilha e pasta de saída, última pasta de entrada e geometria da janela.

**3.2.0 · 28/10/2026:** fluidez adaptativa com slow em 24 fps e rampas/trechos rápidos até 60 fps (padrão), 120 ou 240 fps experimental; alternativa fixa em 24 fps; validação dos timestamps em H.264 e HEVC.

**3.1.0 · 28/10/2026:** ação ao terminar (pasta ou pop-up), espera inicial cancelável de 2 segundos e diálogo de sobrescrever/pular com aplicar a todos. Substituição atômica preserva o destino em caso de falha na renderização.

**3.0.0 · 28/10/2026:** troca do perfil automático pelo slow FPS/24 do novo script; rampas suaves de 0,5 a 5 s; tempo de slow e até quatro blocos, com 1× mantendo slow até o fim. Sony S&Q passa a ser opcional e desmarcado por padrão.

**2.2.0 · 28/10/2026:** janela ampliada, opções compactadas com painel de reprodução do vídeo original, título Fila de edição e emojis nos botões pausar/cancelar.

**2.1.0 · 28/10/2026:** janela mais larga com entrada ampliada à esquerda e opções à direita; barras finas agrupadas; botões iniciar, pausar/retomar e cancelar. Pausa real dos subprocessos e cancelamento seguro durante a pausa.

**2.0.0 · 28/10/2026:** primeira interface visual; entrada por arrastar e seleção filtrada, fade com capa preservada, trilha opcional, velocidade automática/manual, corte por duração e direção, pasta de saída, progresso real do FFmpeg, logs, cancelamento e proteção de arquivos existentes. Mantém o perfil oficial 1.2.0 como referência.
