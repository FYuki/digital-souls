import { readFile } from 'node:fs/promises'

import {
  DEPENDENCY_NAMES,
  PROFILE_REPORT_ENV,
  parseResolvedProfile,
  type Capability,
  type DependencyMode,
  type DependencyName,
  type ResolvedProfile,
} from '../resolved-profile'

export type {
  Capability,
  DependencyMode,
  DependencyName,
  DependencySource,
  ResolvedDependency,
  ResolvedProfile,
} from '../resolved-profile'
export { PROFILE_REPORT_ENV } from '../resolved-profile'

const PROFILE_ANNOTATION_TYPE = 'ds-profile'
const PROFILE_ATTACHMENT_NAME = 'resolved-profile.json'

export const readResolvedProfile = async (): Promise<ResolvedProfile> => {
  const reportPath = process.env[PROFILE_REPORT_ENV]
  if (reportPath === undefined || reportPath.length === 0) {
    throw new Error(`${PROFILE_REPORT_ENV} must identify the resolved profile report`)
  }

  let serialized: string
  try {
    serialized = await readFile(reportPath, 'utf-8')
  } catch (error) {
    throw new Error(`failed to read resolved profile report ${reportPath}`, { cause: error })
  }
  try {
    return parseResolvedProfile(JSON.parse(serialized) as unknown)
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`resolved profile report contains invalid JSON: ${reportPath}`, { cause: error })
    }
    throw error
  }
}

export const getCapabilitySkipReason = (
  report: ResolvedProfile,
  requiredCapability: Capability,
): string | null => {
  if (report.capabilities.includes(requiredCapability)) {
    return null
  }
  const dependencyModes = DEPENDENCY_NAMES.map((name) =>
    `${name}=${report.dependencies[name].mode}/${String(report.dependencies[name].source)}`
  )
  return [
    `profile ${report.effectiveProfile} lacks required capability: ${requiredCapability}`,
    `resolved dependencies: ${dependencyModes.join(', ')}`,
  ].join('; ')
}

const createProfileEvidence = (report: ResolvedProfile) => ({
  profileName: report.effectiveProfile,
  dependencyModes: Object.fromEntries(
    DEPENDENCY_NAMES.map((name) => [name, report.dependencies[name].mode]),
  ) as Record<DependencyName, DependencyMode>,
  capabilities: [...report.capabilities],
})

type ProfileTestInfo = {
  annotations: { type: string; description?: string }[]
  attach: (
    name: string,
    options: { body: string; contentType: string },
  ) => Promise<void>
}

export const attachProfileEvidence = async (
  testInfo: ProfileTestInfo,
  report: ResolvedProfile,
): Promise<void> => {
  testInfo.annotations = [
    ...testInfo.annotations,
    { type: PROFILE_ANNOTATION_TYPE, description: report.effectiveProfile },
  ]
  await testInfo.attach(PROFILE_ATTACHMENT_NAME, {
    body: JSON.stringify(createProfileEvidence(report), null, 2),
    contentType: 'application/json',
  })
}
