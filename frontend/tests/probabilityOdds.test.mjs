import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'

const bundle = await build({ entryPoints: ['src/types.ts'], bundle: true, write: false, format: 'esm', platform: 'node' })
const { probabilityOdds } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

test('direct fair percentages display as equivalent decimal and American odds', () => {
  assert.deepEqual(probabilityOdds('0.60'), { decimal: '1.67', american: '-150' })
  assert.deepEqual(probabilityOdds('0.40'), { decimal: '2.50', american: '+150' })
  assert.deepEqual(probabilityOdds('0.50'), { decimal: '2.00', american: '+100' })
  assert.deepEqual(probabilityOdds('1'), { decimal: '1.00', american: '—' })
})

test('missing or invalid percentages never display invented odds', () => {
  for (const value of [null, undefined, '', 'NaN', 'Infinity', '-0.2', '1.2', 0]) {
    assert.equal(probabilityOdds(value), null)
  }
})
