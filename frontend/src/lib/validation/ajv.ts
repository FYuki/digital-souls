import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'
import type { ValidateFunction } from 'ajv'

// contracts/以下のschemaを検証するvalidatorを統一設定で構築する。
export const compileContractSchema = (schema: object): ValidateFunction => {
  const ajv = new Ajv2020({ allErrors: true, strict: true })
  addFormats(ajv)
  return ajv.compile(schema)
}
