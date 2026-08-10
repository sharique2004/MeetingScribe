<Frame name="AppFrame" flex="row" w={1440} h={900} bg="#0A0A12">
  <Frame name="Sidebar" flex="col" h={900} w={248} gap={16} py={22} px={20} shadow="0 8 30 #00000055">
    <Frame name="Logo" flex="row" w={208} gap={8}>
      <Frame name="logodot" w={22} h={22} bg="#5EEAD4" rounded={7} />
      <Text name="brand" size={15} font="Bricolage Grotesque" weight="bold" color="#F2F3F8">MeetingScribe</Text>
      <Frame name="localpill" flex="col" py={3} px={8} bg="#5EEAD41F" rounded={999}>
        <Text name="lp" size={10} font="JetBrains Mono" weight={600} color="#5EEAD4">local</Text>
      </Frame>
    </Frame>
    <Frame name="NewBtn" flex="row" w={208} gap={8} py={10} px={12} bg="#5EEAD4" rounded={10}>
      <Text name="nb" size={13} font="Hanken Grotesk" weight="bold" color="#04262D">+  New recording</Text>
    </Frame>
    <Frame name="Search" flex="row" w={208} py={9} px={12} bg="#FFFFFF08" stroke="#FFFFFF14" rounded={9}>
      <Text name="sr" size={13} font="Hanken Grotesk" color="#6B7079">Search meetings</Text>
    </Frame>
    <Frame name="Group" flex="col" w={208} gap={4}>
      <Text name="glabel" size={10} font="JetBrains Mono" weight={600} color="#6B7079">TODAY</Text>
      <Frame name="MeetItem" flex="col" w={208} gap={4} py={8} px={10} bg="#FFFFFF10" rounded={8}>
        <Frame name="mi_head" flex="row" w={208} gap={8}>
          <Text name="mtitle" size={13} font="Hanken Grotesk" weight={600} color="#F2F3F8">Q3 Roadmap Sync</Text>
          <Frame name="adot" w={6} h={6} bg="#5EEAD4" rounded={999} />
        </Frame>
        <Text name="mmeta" size={11} font="JetBrains Mono" color="#6B7079">2:14 PM  ·  24:18  ·  3 speakers</Text>
      </Frame>
      <Frame name="MeetItem" flex="col" w={208} gap={4} py={8} px={10} bg="#00000000" rounded={8}>
        <Frame name="mi_head" flex="row" w={208} gap={8}>
          <Text name="mtitle" size={13} font="Hanken Grotesk" weight={600} color="#AEB4C2">Design review — mobile</Text>
        </Frame>
        <Text name="mmeta" size={11} font="JetBrains Mono" color="#6B7079">11:05 AM  ·  38:52  ·  4 speakers</Text>
      </Frame>
    </Frame>
    <Frame name="Group" flex="col" w={208} gap={4}>
      <Text name="glabel" size={10} font="JetBrains Mono" weight={600} color="#6B7079">YESTERDAY</Text>
      <Frame name="MeetItem" flex="col" w={208} gap={4} py={8} px={10} bg="#00000000" rounded={8}>
        <Frame name="mi_head" flex="row" w={208} gap={8}>
          <Text name="mtitle" size={13} font="Hanken Grotesk" weight={600} color="#AEB4C2">1:1 with Priya</Text>
        </Frame>
        <Text name="mmeta" size={11} font="JetBrains Mono" color="#6B7079">4:30 PM  ·  27:41  ·  2 speakers</Text>
      </Frame>
      <Frame name="MeetItem" flex="col" w={208} gap={4} py={8} px={10} bg="#00000000" rounded={8}>
        <Frame name="mi_head" flex="row" w={208} gap={8}>
          <Text name="mtitle" size={13} font="Hanken Grotesk" weight={600} color="#AEB4C2">Customer call — Northwind</Text>
        </Frame>
        <Text name="mmeta" size={11} font="JetBrains Mono" color="#6B7079">1:00 PM  ·  44:09  ·  5 speakers</Text>
      </Frame>
      <Frame name="MeetItem" flex="col" w={208} gap={4} py={8} px={10} bg="#00000000" rounded={8}>
        <Frame name="mi_head" flex="row" w={208} gap={8}>
          <Text name="mtitle" size={13} font="Hanken Grotesk" weight={600} color="#AEB4C2">Eng standup</Text>
        </Frame>
        <Text name="mmeta" size={11} font="JetBrains Mono" color="#6B7079">9:30 AM  ·  12:03  ·  6 speakers</Text>
      </Frame>
    </Frame>
    <Frame name="Group" flex="col" w={208} gap={4}>
      <Text name="glabel" size={10} font="JetBrains Mono" weight={600} color="#6B7079">THIS WEEK</Text>
      <Frame name="MeetItem" flex="col" w={208} gap={4} py={8} px={10} bg="#00000000" rounded={8}>
        <Frame name="mi_head" flex="row" w={208} gap={8}>
          <Text name="mtitle" size={13} font="Hanken Grotesk" weight={600} color="#AEB4C2">Pricing workshop</Text>
        </Frame>
        <Text name="mmeta" size={11} font="JetBrains Mono" color="#6B7079">Wed  ·  58:20  ·  4 speakers</Text>
      </Frame>
      <Frame name="MeetItem" flex="col" w={208} gap={4} py={8} px={10} bg="#00000000" rounded={8}>
        <Frame name="mi_head" flex="row" w={208} gap={8}>
          <Text name="mtitle" size={13} font="Hanken Grotesk" weight={600} color="#AEB4C2">Board prep</Text>
        </Frame>
        <Text name="mmeta" size={11} font="JetBrains Mono" color="#6B7079">Tue  ·  1:02:11  ·  3 speakers</Text>
      </Frame>
    </Frame>
  </Frame>
  <Frame name="Main" flex="col" h={900} w={838}>
    <Frame name="Topbar" flex="row" w={838} gap={12} py={16} px={24}>
      <Text name="rvtitle" size={21} font="Bricolage Grotesque" weight="bold" color="#F2F3F8">Q3 Roadmap Sync</Text>
      <Frame name="sp" w={346} h={1} grow={1} bg="#00000000" />
      <Frame name="CmdK" flex="row" py={7} px={10} bg="#FFFFFF08" stroke="#FFFFFF14" rounded={8}>
        <Text name="ck" size={12} font="JetBrains Mono" color="#AEB4C2">⌘K</Text>
      </Frame>
      <Frame name="Aa" flex="row" gap={10} py={7} px={11} bg="#FFFFFF08" stroke="#FFFFFF14" rounded={8}>
        <Text name="aa" size={12} font="JetBrains Mono" color="#AEB4C2">A−   A+</Text>
      </Frame>
      <Frame name="SummaryBtn" flex="row" gap={8} py={9} px={15} bg="#5EEAD4" stroke="#7DD3FC" strokeWidth={1.5} rounded={10} shadow="0 0 18 #5EEAD455">
        <Text name="sb" size={13} font="Hanken Grotesk" weight="bold" color="#04262D">✦  Summary</Text>
      </Frame>
    </Frame>
    <Frame name="Meta" flex="row" w={838} gap={8} py={0} px={24}>
      <Frame name="chip" flex="col" py={4} px={9} bg="#FFFFFF08" rounded={999}>
        <Text name="cp" size={11} font="JetBrains Mono" color="#AEB4C2">Today · 2:14 PM</Text>
      </Frame>
      <Frame name="chip" flex="col" py={4} px={9} bg="#FFFFFF08" rounded={999}>
        <Text name="cp" size={11} font="JetBrains Mono" color="#AEB4C2">24:18</Text>
      </Frame>
      <Frame name="chip" flex="col" py={4} px={9} bg="#FFFFFF08" rounded={999}>
        <Text name="cp" size={11} font="JetBrains Mono" color="#AEB4C2">Online call</Text>
      </Frame>
      <Frame name="chip" flex="col" py={4} px={9} bg="#FFFFFF08" rounded={999}>
        <Text name="cp" size={11} font="JetBrains Mono" color="#AEB4C2">Whisper · GPU</Text>
      </Frame>
    </Frame>
    <Frame name="Transcript" flex="col" h={560} w={838} gap={6} py={16} px={24}>
      <Frame name="Turn" flex="row" w={790} gap={16} py={12} px={16} rounded={12}>
        <Frame name="tsgut" w={44} h={100}>
          <Text name="ts" size={12} font="JetBrains Mono" color="#6B7079">0:03</Text>
        </Frame>
        <Frame name="turncol" flex="col" w={690} gap={6}>
          <Text name="spk" size={12} font="Hanken Grotesk" weight="bold" color="#5B8CFF">You</Text>
          <Text name="line" w={667} size={15} font="Hanken Grotesk" lineHeight={26} color="#AEB4C2">Okay, we're recording. Let's keep this to the three things that block Q3: pricing, the mobile beta, and data-retention. Priya, where did pricing land?</Text>
        </Frame>
      </Frame>
      <Frame name="Turn" flex="row" w={790} gap={16} py={12} px={16} rounded={12}>
        <Frame name="tsgut" w={44} h={100}>
          <Text name="ts" size={12} font="JetBrains Mono" color="#6B7079">0:19</Text>
        </Frame>
        <Frame name="turncol" flex="col" w={690} gap={6}>
          <Text name="spk" size={12} font="Hanken Grotesk" weight="bold" color="#C07AF6">Priya Nair</Text>
          <Text name="line" w={673} size={15} font="Hanken Grotesk" lineHeight={26} color="#AEB4C2">After the customer calls, the usage-based tier is the one people keep asking for. Everyone above ~40 seats wants to pay for what they actually use.</Text>
        </Frame>
      </Frame>
      <Frame name="Turn_active" flex="row" w={790} gap={16} py={12} px={16} bg="#5EEAD40A" rounded={12} shadow="0 0 16 #5EEAD433">
        <Frame name="tsgut" w={44} h={100}>
          <Text name="ts" size={12} font="JetBrains Mono" color="#6B7079">1:02</Text>
        </Frame>
        <Frame name="turncol" flex="col" w={690} gap={6}>
          <Text name="spk" size={12} font="Hanken Grotesk" weight="bold" color="#2FBF87">Marcus Bell</Text>
          <Text name="line" w={673} size={15} font="Hanken Grotesk" lineHeight={26} color="#F2F3F8">Metering isn't the hard part, we already emit usage events. The hard part is making them trustworthy enough to put on an invoice. I'd want two weeks for reconciliation.</Text>
        </Frame>
      </Frame>
      <Frame name="Turn" flex="row" w={790} gap={16} py={12} px={16} rounded={12}>
        <Frame name="tsgut" w={44} h={100}>
          <Text name="ts" size={12} font="JetBrains Mono" color="#6B7079">1:28</Text>
        </Frame>
        <Frame name="turncol" flex="col" w={690} gap={6}>
          <Text name="spk" size={12} font="Hanken Grotesk" weight="bold" color="#5B8CFF">You</Text>
          <Text name="line" w={666} size={15} font="Hanken Grotesk" lineHeight={26} color="#AEB4C2">Two weeks is fine if it means we don't refund half the first month. Let's treat trustworthy metering as the gate for the pricing launch.</Text>
        </Frame>
      </Frame>
      <Frame name="Turn" flex="row" w={790} gap={16} py={12} px={16} rounded={12}>
        <Frame name="tsgut" w={44} h={100}>
          <Text name="ts" size={12} font="JetBrains Mono" color="#6B7079">2:34</Text>
        </Frame>
        <Frame name="turncol" flex="col" w={690} gap={6}>
          <Text name="spk" size={12} font="Hanken Grotesk" weight="bold" color="#C07AF6">Priya Nair</Text>
          <Text name="line" w={668} size={15} font="Hanken Grotesk" lineHeight={26} color="#AEB4C2">So the deliverable is two things: a written retention policy, and a purge audit trail we can show. I can draft the policy with legal this week.</Text>
        </Frame>
      </Frame>
    </Frame>
    <Frame name="Transport" flex="row" w={838} gap={14} py={14} px={24} bg="#0C0C16">
      <Frame name="Play" w={34} h={34} bg="#5EEAD4" rounded={999}>
        <Text name="pl" size={13} font="Hanken Grotesk" color="#04262D">▶</Text>
      </Frame>
      <Text name="clk" size={12} font="JetBrains Mono" color="#AEB4C2">3:41 / 24:18</Text>
      <Frame name="Wave" flex="row" w={420} h={34} gap={3}>
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#5EEAD4" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={8} bg="#2A2A3A" rounded={2} />
        <Frame name="wb" w={3} h={21} bg="#2A2A3A"