export type DependencyMode = 'real' | 'mock' | 'disabled'
export type DependencySource = 'managed' | 'external' | 'in_process' | 'browser' | null
export type DependencyName = 'frontend' | 'backend' | 'ollama' | 'voicevox' | 'whisper' | 'chroma'
export type Capability = 'mocked-e2e' | 'text-chat-real' | 'voice-chat-real' | 'rag-real'

export const PROFILE_REPORT_ENV = 'DS_PROFILE_REPORT'
export const BACKEND_ORIGIN_ENV = 'DS_BACKEND_ORIGIN'

export type ResolvedDependency = {
  mode: DependencyMode
  source: DependencySource
  baseUrl?: string
  readinessPath?: string
  readinessUrl?: string
}

export type ResolvedProfile = {
  reportSchemaVersion: 1
  effectiveProfile: string
  dependencies: Record<DependencyName, ResolvedDependency>
  capabilities: Capability[]
}

export const DEPENDENCY_NAMES = Object.freeze([
  'frontend',
  'backend',
  'ollama',
  'voicevox',
  'whisper',
  'chroma',
] as const satisfies readonly DependencyName[])

const CAPABILITY_NAMES = Object.freeze([
  'mocked-e2e',
  'text-chat-real',
  'voice-chat-real',
  'rag-real',
] as const satisfies readonly Capability[])

const DEPENDENCY_MODES = new Set<unknown>(['real', 'mock', 'disabled'])
const DEPENDENCY_SOURCES = new Set<unknown>([
  'managed',
  'external',
  'in_process',
  'browser',
  null,
])
const CAPABILITY_NAME_SET = new Set<unknown>(CAPABILITY_NAMES)

const requireRecord = (value: unknown, path: string): Record<string, unknown> => {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${path} must be an object`)
  }
  return value as Record<string, unknown>
}

const parseDependency = (value: unknown, path: string): ResolvedDependency => {
  const dependency = requireRecord(value, path)
  if (!DEPENDENCY_MODES.has(dependency.mode)) {
    throw new Error(`${path}.mode is invalid`)
  }
  if (!DEPENDENCY_SOURCES.has(dependency.source)) {
    throw new Error(`${path}.source is invalid`)
  }
  for (const field of ['baseUrl', 'readinessPath', 'readinessUrl'] as const) {
    if (field in dependency && typeof dependency[field] !== 'string') {
      throw new Error(`${path}.${field} must be a string`)
    }
  }
  return {
    mode: dependency.mode as DependencyMode,
    source: dependency.source as DependencySource,
    ...typeof dependency.baseUrl === 'string' ? { baseUrl: dependency.baseUrl } : {},
    ...typeof dependency.readinessPath === 'string' ? { readinessPath: dependency.readinessPath } : {},
    ...typeof dependency.readinessUrl === 'string' ? { readinessUrl: dependency.readinessUrl } : {},
  }
}

const parseDependencies = (value: unknown): Record<DependencyName, ResolvedDependency> => {
  const rawDependencies = requireRecord(value, 'dependencies')
  for (const name of DEPENDENCY_NAMES) {
    if (!(name in rawDependencies)) {
      throw new Error(`dependencies.${name} is required`)
    }
  }
  return Object.fromEntries(
    DEPENDENCY_NAMES.map((name) => [
      name,
      parseDependency(rawDependencies[name], `dependencies.${name}`),
    ]),
  ) as Record<DependencyName, ResolvedDependency>
}

const parseCapabilities = (value: unknown): Capability[] => {
  if (!Array.isArray(value)) {
    throw new Error('capabilities must be an array')
  }
  const unknownCapability = value.find((item) => !CAPABILITY_NAME_SET.has(item))
  if (unknownCapability !== undefined) {
    throw new Error(`capabilities contains unknown value: ${String(unknownCapability)}`)
  }
  return value as Capability[]
}

export const parseResolvedProfile = (value: unknown): ResolvedProfile => {
  const report = requireRecord(value, 'resolved profile report')
  if (report.reportSchemaVersion !== 1) {
    throw new Error(`unsupported reportSchemaVersion: ${String(report.reportSchemaVersion)}`)
  }
  if (typeof report.effectiveProfile !== 'string' || report.effectiveProfile.length === 0) {
    throw new Error('effectiveProfile must be a non-empty string')
  }
  return {
    reportSchemaVersion: 1,
    effectiveProfile: report.effectiveProfile,
    dependencies: parseDependencies(report.dependencies),
    capabilities: parseCapabilities(report.capabilities),
  }
}
