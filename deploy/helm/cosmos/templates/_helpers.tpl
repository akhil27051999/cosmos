{{/*
Naming + label helpers. Every resource name is prefixed with the release name via
`cosmos.fullname`, so two installs into one namespace don't collide.
*/}}

{{- define "cosmos.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cosmos.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "cosmos.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels shared by every object (Helm/k8s recommended set). */}}
{{- define "cosmos.labels" -}}
helm.sh/chart: {{ include "cosmos.chart" . }}
app.kubernetes.io/name: {{ include "cosmos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels for ONE component. Call as:
  {{- include "cosmos.selectorLabels" (dict "ctx" . "component" "gateway") }}
Selector labels must be stable (they're immutable on a Deployment), so they
exclude version/chart which change on upgrade.
*/}}
{{- define "cosmos.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cosmos.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* The Secret name the workloads read env from (existing or chart-created). */}}
{{- define "cosmos.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "cosmos.fullname" .) -}}
{{- end -}}
{{- end -}}
