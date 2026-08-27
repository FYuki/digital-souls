export type Observation = Readonly<{
  name: string
  value: number
  clockDomain: string
  unit: string
}>

export const elapsed = (end: Observation, start: Observation): Observation => {
  if (end.clockDomain !== start.clockDomain || end.unit !== start.unit) {
    throw new Error('Observations must share a clock domain and unit')
  }
  return {
    name: 'ttfa',
    value: end.value - start.value,
    clockDomain: end.clockDomain,
    unit: end.unit,
  }
}

