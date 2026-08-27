<script lang="ts">
  import { getInitialToken, getReconnectToken, type TokenResponse } from './client'
  import { LiveKitRoomClient, type RoomObservation } from './room'

  let binding: TokenResponse | null = null
  let transport = 'idle'
  let control = 'unavailable'
  let audio = 'unavailable'
  let generation = 0
  let microphoneFrames = 0
  let microphoneSamples = 0
  let characterRenderedSamples = 0
  let playedPrefix = -1
  let duplicateTrackFrames = 0
  let activeAudioGraphs = 0
  let renderedEnergy = 0
  let confirmedSegments = 0
  let unassignedRenderedSamples = 0
  let acknowledgedPlaybackPrefix = -1
  let terminalResponseId = ''
  let terminalConfirmedAudioSequence = 0
  let activeResponseId = ''
  let errorMessage = ''
  let conversationId: string | null = null
  let pendingInitialToken: Promise<{ conversationId: string; token: TokenResponse }> | null = null
  const roomClient = new LiveKitRoomClient((observation: RoomObservation) => {
    transport = observation.transport
    control = observation.control
    audio = observation.audio
    if (observation.renderedSamples !== undefined) {
      characterRenderedSamples = observation.renderedSamples
    }
    if (observation.generation !== undefined) generation = observation.generation
    if (observation.playedPrefix !== undefined) playedPrefix = observation.playedPrefix
    if (observation.microphoneFrames !== undefined) microphoneFrames = observation.microphoneFrames
    if (observation.microphoneSamples !== undefined) microphoneSamples = observation.microphoneSamples
    if (observation.duplicateTrackFrames !== undefined) duplicateTrackFrames = observation.duplicateTrackFrames
    if (observation.activeAudioGraphs !== undefined) activeAudioGraphs = observation.activeAudioGraphs
    if (observation.renderedEnergy !== undefined) renderedEnergy = observation.renderedEnergy
    if (observation.confirmedSegments !== undefined) confirmedSegments = observation.confirmedSegments
    if (observation.unassignedRenderedSamples !== undefined) {
      unassignedRenderedSamples = observation.unassignedRenderedSamples
    }
    if (observation.acknowledgedPlaybackPrefix !== undefined) {
      acknowledgedPlaybackPrefix = observation.acknowledgedPlaybackPrefix
    }
    if (observation.terminalResponseId !== undefined) {
      terminalResponseId = observation.terminalResponseId
    }
    if (observation.terminalConfirmedAudioSequence !== undefined) {
      terminalConfirmedAudioSequence = observation.terminalConfirmedAudioSequence
    }
    if (observation.activeResponseId !== undefined) {
      activeResponseId = observation.activeResponseId
    }
  })

  const obtainToken = async (): Promise<void> => {
    errorMessage = ''
    try {
      pendingInitialToken = getInitialToken()
      const initial = await pendingInitialToken
      conversationId = initial.conversationId
      binding = initial.token
    } catch (error) {
      errorMessage = error instanceof Error ? error.message : 'token request failed'
    }
  }

  const obtainReconnectToken = async (): Promise<void> => {
    if (binding === null && pendingInitialToken !== null) {
      const initial = await pendingInitialToken
      conversationId = initial.conversationId
      binding = initial.token
    }
    if (binding === null || conversationId === null) return
    errorMessage = ''
    try {
      binding = await getReconnectToken(conversationId, binding.session_id)
    } catch (error) {
      errorMessage = error instanceof Error ? error.message : 'token request failed'
    }
  }

  const connect = async (): Promise<void> => {
    if (binding === null) return
    await roomClient.connect(binding.livekit_url, binding.token, binding.session_id)
  }

  const disconnect = (): void => {
    roomClient.disconnect()
  }

  const temporaryDisconnect = (): void => {
    roomClient.temporaryDisconnect()
  }

  const reconnect = async (): Promise<void> => {
    await obtainReconnectToken()
    if (binding !== null) {
      await connect()
    }
  }
</script>

<main>
  <h1>LiveKit 音声transport実験</h1>
  <p>requested reconnect grace: 60000 ms</p>
  <button on:click={obtainToken}>token取得</button>
  <button on:click={obtainReconnectToken}>再接続token取得</button>
  <button on:click={connect} disabled={binding === null}>Room接続</button>
  <button on:click={disconnect}>切断</button>
  <button on:click={async () => { await roomClient.publishMicrophone() }} disabled={transport !== 'available'}>microphone開始</button>
  <button on:click={temporaryDisconnect}>一時切断</button>
  <button on:click={reconnect} disabled={binding === null}>再接続</button>
  {#if binding !== null}<p data-testid="session-id">{binding.session_id}</p>{/if}
  {#if conversationId !== null}<p data-testid="conversation-id">{conversationId}</p>{/if}
  <p>transport: {transport}</p>
  <p>control: {control}</p>
  <p>audio: {audio}</p>
  <p>generation: {generation}</p>
  <p>microphone frames: {microphoneFrames}</p>
  <p>microphone samples: {microphoneSamples}</p>
  <p>character rendered samples: {characterRenderedSamples}</p>
  <p>played prefix: {playedPrefix}</p>
  <p>duplicate track frames: {duplicateTrackFrames}</p>
  <p>active audio graphs: {activeAudioGraphs}</p>
  <p>character rendered energy: {renderedEnergy}</p>
  <p>confirmed segments: {confirmedSegments}</p>
  <p>unassigned rendered samples: {unassignedRenderedSamples}</p>
  <p>acknowledged playback prefix: {acknowledgedPlaybackPrefix}</p>
  <p>terminal response ID: {terminalResponseId}</p>
  <p>terminal confirmed audio sequence: {terminalConfirmedAudioSequence}</p>
  <p>active response ID: {activeResponseId}</p>
  {#if errorMessage}<p role="alert">{errorMessage}</p>{/if}
</main>
