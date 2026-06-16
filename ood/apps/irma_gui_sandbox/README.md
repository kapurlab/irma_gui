# IRMA — OOD sandbox app (Slurm, no admin)

Per-user deployment of IRMA on any Open OnDemand HPC. The card appears
under **Develop → My Sandbox Apps** for your account only — no root, no
`/var/www` access.

Install it with the umbrella CLI:

```bash
git clone https://github.com/kapurlab/bioinformatic_diagnostic_tools.git
cd bioinformatic_diagnostic_tools
bin/bdtools install --sandbox irma_gui        # builds env+frontend, links this card
$EDITOR ~/.../ood/apps/irma_gui_sandbox/form.yml   # set cluster: "CHANGE_ME" -> your cluster
```

`bdtools install --sandbox irma_gui` writes `~/.config/irma_gui/sandbox.env`
(`BDTOOLS_APP_DIR` + `BDTOOLS_APP_ENV`) and symlinks this card into
`~/ondemand/dev/irma_gui`. The launcher (`template/script.sh.erb`) sources
that file, so the checkout + conda env can live anywhere in your $HOME.

| File | Runs where | Job |
|---|---|---|
| `form.yml` | dashboard | cluster + Slurm resource fields |
| `submit.yml.erb` | OOD submit | sbatch directives |
| `template/before.sh` | compute node | `find_port` for uvicorn |
| `template/script.sh.erb` | compute node | sources sandbox.env, starts uvicorn |
| `view.html.erb` | dashboard | the Open button |
