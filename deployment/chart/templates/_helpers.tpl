{{- define "abrl.applicationNode" -}}
nodeSelector:
  abrl.io/role: app
{{- end }}
{{- define "abrl.image" -}}
{{- if .digest -}}
{{ .repository }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end }}
{{- define "abrl.spread" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector: {matchLabels: {app: {{ . }}}}
{{- end }}
{{- define "abrl.dataNode" -}}
nodeSelector:
  abrl.io/role: data
tolerations:
  - key: abrl.io/data
    operator: Equal
    value: "true"
    effect: NoSchedule
{{- end }}
{{- define "abrl.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile: {type: RuntimeDefault}
{{- end }}
