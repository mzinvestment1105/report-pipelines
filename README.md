# report-pipelines

Public pipelines for daily ETL and report generation. Outputs are pushed back to a private repository for personal use.

## Workflows

- **screening_master**: Daily ETL using JQuants API → parquet/xlsx artifacts
- **macro_report_daily**: Daily macro market report generation via Claude Code Action

## Structure

```
bi/pipelines/      Python scripts for ETL and report generation
prompts/           Prompt templates for claude-code-action
.github/workflows/ GitHub Actions definitions
```

## License

Personal use only. No external contributions accepted.
