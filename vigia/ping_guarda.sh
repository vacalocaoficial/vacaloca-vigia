#!/usr/bin/env bash
# RELOGIO DE GUARDA (aviso externo ao Healthchecks.io). Roda DEPOIS do vigia, nunca decide o resultado dele.
# - Nunca imprime a chave. Sem a chave (secret ausente): so avisa que nao esta configurado e sai com 0.
# - Qualquer problema no envio vira AVISO e o script sai com 0 (o passo ainda e continue-on-error no workflow).
# - O resultado do vigia ja foi decidido no passo anterior (falha la = job reprovado, aconteca o que acontecer aqui).
# Entradas (variaveis de ambiente): HC_PING_KEY (secret), RESULTADO (success|failure|skipped|cancelled), RC (codigo do vigia),
#   EVENTO (schedule|workflow_dispatch), SCHEDULE (o cron que disparou, em UTC), ARMAR ("true" so em disparo manual).
# Verificacoes no Healthchecks (nomes = slugs):
#   vigia-0157 (28/11 01:57), vigia-madrugada (28/11 02:17-08:47), vigia-dia28 (28/11 09:47-23:47), vigia-geral-nov (29/11-30/11), vigia-geral-dez (01/12-02/12),
#   vigia-ensaio (disparos manuais comuns; nao alarma ninguem de verdade).
set -u
CHAVE="${HC_PING_KEY:-}"
if [ -z "$CHAVE" ]; then
  echo "::notice::relogio de guarda nao configurado (secret HC_PING_KEY ausente); nada enviado"
  exit 0
fi
case "$CHAVE" in
  *[!A-Za-z0-9_-]*) echo "::warning::HC_PING_KEY com caracteres inesperados; nada enviado"; exit 0 ;;
esac

RESULTADO="${RESULTADO:-}"
RC="${RC:-}"
if [ "$RESULTADO" = "success" ]; then
  STATUS=0
else
  case "$RC" in
    ''|*[!0-9]*|0) STATUS=1 ;;
    *) STATUS=$((10#$RC)); [ "$STATUS" -gt 255 ] && STATUS=1 ;;
  esac
fi

SLUGS="vigia-ensaio"
if [ "${EVENTO:-}" = "schedule" ]; then
  # a fase vem do CRON que disparou (nao da hora real), para um atraso do GitHub nao trocar de verificacao
  IFS=' ' read -r MI HR DD MM _ <<< "${SCHEDULE:-}"
  case "$MI$HR$DD$MM" in
    ''|*[!0-9]*) echo "::warning::cron ilegivel; ping so para vigia-ensaio" ;;
    *)
      # chave AAAAMMDDHHMM em UTC (o ano fixo 2026 serve: o cron nao tem ano e, em 2027, o vigia ja encerrou)
      K=$(printf '2026%02d%02d%02d%02d' "$((10#$MM))" "$((10#$DD))" "$((10#$HR))" "$((10#$MI))")
      if   [ "$K" -le 202611280457 ]; then SLUGS="vigia-0157"          # 01:57 BRT = 04:57 UTC
      elif [ "$K" -lt 202611281200 ]; then SLUGS="vigia-madrugada"     # ate 08:47 BRT
      elif [ "$K" -lt 202611290300 ]; then SLUGS="vigia-dia28"         # 09:47 a 23:47 BRT de 28/11
      elif [ "$K" -lt 202612010300 ]; then SLUGS="vigia-geral-nov"     # 29/11 00:47 a 30/11 21:47 BRT (este e 01/12 00:47 UTC)
      else SLUGS="vigia-geral-dez"; fi ;;                              # 01/12 00:47 BRT (03:47 UTC) em diante
  esac
elif [ "${EVENTO:-}" = "workflow_dispatch" ] && [ "${ARMAR:-}" = "true" ]; then
  if [ "$STATUS" -eq 0 ]; then
    SLUGS="vigia-0157 vigia-madrugada vigia-dia28 vigia-geral-nov vigia-geral-dez"   # "arma" as 5 verificacoes reais (uma verificacao nova nunca alerta antes do 1o ping)
  else
    echo "::warning::armar so vale com o vigia aprovado; verificacoes reais nao foram armadas"
  fi
fi

for S in $SLUGS; do
  SAIDA=$(curl -fsS -m 15 --retry 3 --retry-delay 2 -o /dev/null "https://hc-ping.com/${CHAVE}/${S}/${STATUS}" 2>&1) \
    && echo "relogio de guarda: ${S} <- status ${STATUS}" \
    || echo "::warning::relogio de guarda: ping de ${S} NAO enviado (${SAIDA//${CHAVE}/***})"
done
exit 0
