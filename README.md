# Mihomo rules

Reviewed rule providers for Mihomo.

## Available rules

- `process/games-direct.yaml` — Windows game processes routed directly.

## Usage

```yaml
rule-providers:
  games-processes:
    type: http
    behavior: classical
    format: yaml
    url: https://raw.githubusercontent.com/Erimelt1/mihomo-rules/refs/heads/main/process/games-direct.yaml
    path: ./rule-sets/games-direct.yaml
    interval: 86400

rules:
  - RULE-SET,games-processes,DIRECT
```
