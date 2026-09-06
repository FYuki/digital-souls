import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  FetchingJSONSchemaStore,
  InputData,
  JSONSchemaInput,
  quicktype,
} from 'quicktype-core'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repositoryRoot = resolve(frontendRoot, '..')
const schemaPath = resolve(
  repositoryRoot,
  'contracts/perception/screen/screen-perception.schema.json',
)
const typescriptOutputPath = resolve(
  frontendRoot,
  'src/lib/screen-perception/generated.ts',
)
const pythonOutputPath = resolve(
  repositoryRoot,
  'backend/app/screen_perception/generated.py',
)

const schema = JSON.parse(await readFile(schemaPath, 'utf-8'))
if (!Array.isArray(schema.oneOf) || typeof schema.$defs !== 'object') {
  throw new Error('screen perception schema must define oneOf variants and $defs')
}

const variantNames = schema.oneOf.map((entry) => {
  if (typeof entry?.$ref !== 'string') {
    throw new Error('every screen perception variant must be a $ref')
  }
  const variantName = entry.$ref.split('/').at(-1)
  if (!variantName || schema.$defs[variantName] === undefined) {
    throw new Error(`screen perception variant is not defined: ${entry.$ref}`)
  }
  return variantName
})

if (new Set(variantNames).size !== variantNames.length) {
  throw new Error('screen perception variants must be unique')
}

const variantProperties = Object.fromEntries(
  variantNames.map((variantName) => [
    variantName,
    { $ref: `#/$defs/${variantName}` },
  ]),
)
const generationSchema = {
  $schema: schema.$schema,
  title: 'ScreenPerceptionVariants',
  type: 'object',
  additionalProperties: false,
  required: variantNames,
  properties: variantProperties,
  $defs: schema.$defs,
}

async function render(language, rendererOptions) {
  const inputData = new InputData()
  const schemaInput = new JSONSchemaInput(new FetchingJSONSchemaStore())
  await schemaInput.addSource({
    name: 'ScreenPerceptionVariants',
    schema: JSON.stringify(generationSchema),
  })
  inputData.addInput(schemaInput)
  const result = await quicktype({
    inputData,
    lang: language,
    inferDateTimes: false,
    rendererOptions,
  })
  return `${result.lines.join('\n')}\n`
}

function removeTypescriptWrapper(source) {
  const withoutWrapper = source.replace(
    /^export interface ScreenPerceptionVariants \{[\s\S]*?\}\n\n/,
    '',
  )
  if (withoutWrapper === source) {
    throw new Error('generated TypeScript wrapper was not found')
  }
  return variantNames.reduce(
    (output, variantName) => output.replaceAll(`${variantName}Class`, variantName),
    withoutWrapper,
  )
}

function removePythonWrapper(source) {
  const marker = '\n\nclass ScreenPerceptionVariants(BaseModel):'
  const wrapperIndex = source.indexOf(marker)
  if (wrapperIndex < 0) {
    throw new Error('generated Python wrapper was not found')
  }
  const withoutWrapper = source.slice(0, wrapperIndex)
  const renamed = variantNames.reduce(
    (output, variantName) => output.replaceAll(`${variantName}Class`, variantName),
    withoutWrapper,
  )
  return renamed.replace(
    /^from typing import ([^\n]+)$/m,
    (_, imports) => {
      const names = new Set(imports.split(',').map((name) => name.trim()))
      names.add('Union')
      return `from typing import ${[...names].join(', ')}`
    },
  )
}

const typescriptGenerated = removeTypescriptWrapper(await render('typescript', {
  'just-types': 'true',
  'prefer-unions': 'true',
  'prefer-const-values': 'true',
}))
const typescriptUnion = [
  '/**',
  ' * 画面共有の制御metadata契約。画像本文、質問本文、Vision観測本文、対象名は含めない。',
  ' */',
  'export type ScreenPerceptionEvent =',
  ...variantNames.map((variantName, index) => (
    `  | ${variantName}${index === variantNames.length - 1 ? ';' : ''}`
  )),
  '',
  '',
].join('\n')

const pythonGenerated = removePythonWrapper(await render('python', {
  'just-types': 'true',
  'pydantic-base-model': 'true',
  'python-version': '3.7',
}))
const pythonUnion = [
  '',
  '',
  'ScreenPerceptionEvent = Union[',
  ...variantNames.map((variantName) => `    ${variantName},`),
  ']',
  '',
].join('\n')

await Promise.all([
  writeFile(
    typescriptOutputPath,
    `${typescriptUnion}${typescriptGenerated}`.trimEnd() + '\n',
    'utf-8',
  ),
  writeFile(
    pythonOutputPath,
    `${pythonGenerated}${pythonUnion}`.trimEnd() + '\n',
    'utf-8',
  ),
])
