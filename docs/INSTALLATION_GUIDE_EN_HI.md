# SaleWell Smart Tank Installation Guide

English and Hindi instructions for customers and installers.

> Safety first: A qualified electrician must handle mains electricity, pump wiring, the contactor, earthing, and circuit protection. Switch off power before opening a panel. Never bypass the pump's existing safety controls.

## English

### 1. Before the visit

Keep these details ready:

- property address and contact number;
- number and type of tanks;
- tank height and capacity;
- pump type, power rating, and location;
- source of water: borewell, source tank, municipal supply, or another source;
- photo of the pump panel and tank area;
- local-only or internet/cloud access requirement.

### 2. Check the supplied parts

Check the approved quotation and confirm the required items are available:

- tank sensor node;
- main controller;
- correct power supplies;
- pump relay or contactor interface;
- mounting parts and weatherproof enclosure;
- optional source sensor, valves, water-quality sensor, or extra tank nodes.

Do not install an optional device that is not included in the approved setup.

### 3. Make the site safe

1. Switch off pump and panel power.
2. Confirm that the tank area is safe to reach.
3. Keep electronics away from water and direct rain.
4. Use proper earthing, fuse/MCB protection, cable glands, and insulated terminals.
5. Keep the existing manual OFF control working.

### 4. Install the tank sensor

1. Mount the sensor above the water surface.
2. Point it straight down.
3. Keep it away from the inlet stream, tank wall, ladder, float valve, and pipe.
4. Fix the node firmly so vibration cannot change its angle.
5. Measure the usable tank height and enter the correct value during setup.

### 5. Install the controller and pump interface

1. Mount the main controller near the pump panel in a dry, ventilated place.
2. Connect low-voltage sensors only to their marked terminals.
3. Use a correctly rated contactor for the pump load.
4. Connect controller outputs only to the approved control circuit.
5. Do not connect a high-current pump directly to a small controller relay.
6. Confirm that the pump remains OFF when the controller starts or loses power.

For an existing latch-style motor starter/contactor panel, use two independent isolated relay channels:

- **START relay:** connect its `COM–NO` dry contact in parallel with the physical green START button. It closes for a configurable 300–1000 ms pulse (500 ms default), then releases. The panel's existing auxiliary holding contact keeps the contactor ON.
- **STOP relay:** connect its `COM–NC` dry contact in series with the physical red STOP button, overload, and safety/interlock chain. It opens for a configurable 300–1000 ms pulse (500 ms default), then returns closed.
- Never keep either relay energized continuously. Never bypass the physical buttons, overload, holding contact, MCB/RCCB/ELCB, or another interlock.

> In a standard three-wire starter circuit, START is a parallel NO contact and STOP is a series NC contact. The electrician must verify the actual panel drawing before connection; do not assume terminal numbers from this guide.

### 6. Power on and connect

1. Recheck every terminal and enclosure.
2. Restore controller power first.
3. Confirm that the correct device ID appears.
4. Complete Wi-Fi or local-phone setup when included in the plan.
5. Confirm that the tank node can communicate with the main controller.
6. Confirm cloud status only when cloud service is included.

### 7. Calibrate the tank

1. Enter the tank capacity and usable height.
2. Compare the displayed level with the actual water level.
3. Check the reading when the water is still.
4. Correct sensor position before changing calibration values.
5. Save the approved automatic start and stop levels.

### 8. Test operation

Complete every applicable test:

- tank level changes correctly;
- pump starts only when allowed;
- pump stops at the configured level;
- dry-run/source-water protection blocks unsafe operation;
- physical OFF control stops the pump;
- alerts appear when tested;
- local app access works when included;
- remote/cloud access works when included;
- all installed tanks, sensors, and valves match the approved setup.

Never test a pump without water or bypass a safety lock.

### 9. Customer handover

Show the customer:

- current tank level and pump status;
- Auto and Manual mode behaviour;
- how to use the physical OFF control;
- how to open the app or dashboard;
- which features need internet;
- what to do after a router or password change;
- device ID and SaleWell support details.

### 10. If something is wrong

- **Wrong tank level:** check sensor angle, obstruction, inlet splash, and tank calibration.
- **Pump does not start:** check source water, tank level, mode, safety lock, contactor, and power.
- **Pump does not stop:** use the physical OFF control or safely isolate power, then call an electrician or SaleWell support.
- **Remote data is old:** check controller power and internet. Local automation may continue.
- **App cannot connect locally:** check controller power, phone connection, permissions, and selected device.

Support: **support@salewell.co.in** · **+91 87964 52878**

---

## हिन्दी

### 1. इंस्टॉलेशन से पहले

ये जानकारी तैयार रखें:

- घर या साइट का पता और संपर्क नंबर;
- टैंकों की संख्या और प्रकार;
- टैंक की ऊँचाई और क्षमता;
- पंप का प्रकार, पावर और स्थान;
- पानी का स्रोत: बोरवेल, सोर्स टैंक, नगर निगम सप्लाई या अन्य स्रोत;
- पंप पैनल और टैंक की फोटो;
- केवल लोकल या इंटरनेट/क्लाउड उपयोग की जरूरत।

### 2. दिए गए सामान की जाँच करें

मंजूर कोटेशन के अनुसार जरूरी सामान जाँचें:

- टैंक सेंसर नोड;
- मेन कंट्रोलर;
- सही पावर सप्लाई;
- पंप रिले या कॉन्टैक्टर इंटरफेस;
- माउंटिंग सामान और वाटरप्रूफ बॉक्स;
- जरूरत के अनुसार सोर्स सेंसर, वाल्व, पानी की गुणवत्ता सेंसर या अतिरिक्त टैंक नोड।

जो वैकल्पिक डिवाइस मंजूर सेटअप में नहीं है, उसे इंस्टॉल न करें।

### 3. साइट को सुरक्षित बनाएँ

1. पंप और पैनल की बिजली बंद करें।
2. टैंक तक सुरक्षित पहुँच की जाँच करें।
3. इलेक्ट्रॉनिक सामान को पानी और सीधी बारिश से बचाएँ।
4. सही अर्थिंग, फ्यूज/MCB, केबल ग्लैंड और इंसुलेटेड टर्मिनल लगाएँ।
5. पंप का मौजूदा मैनुअल OFF बटन चालू रखें।

### 4. टैंक सेंसर लगाएँ

1. सेंसर को पानी की सतह के ऊपर लगाएँ।
2. सेंसर का मुँह सीधा नीचे रखें।
3. सेंसर को इनलेट पानी, टैंक की दीवार, सीढ़ी, फ्लोट वाल्व और पाइप से दूर रखें।
4. सेंसर को मजबूती से लगाएँ ताकि कंपन से उसका कोण न बदले।
5. टैंक की उपयोगी ऊँचाई नापकर सेटअप में सही मान डालें।

### 5. कंट्रोलर और पंप इंटरफेस लगाएँ

1. मेन कंट्रोलर को पंप पैनल के पास सूखी और हवादार जगह पर लगाएँ।
2. लो-वोल्टेज सेंसर को केवल चिन्हित टर्मिनल से जोड़ें।
3. पंप की क्षमता के अनुसार सही कॉन्टैक्टर लगाएँ।
4. कंट्रोलर आउटपुट को केवल मंजूर कंट्रोल सर्किट से जोड़ें।
5. बड़े पंप को छोटे कंट्रोलर रिले से सीधे न चलाएँ।
6. जाँचें कि कंट्रोलर चालू होने या उसकी बिजली जाने पर पंप OFF रहे।

### 6. बिजली चालू करें और कनेक्ट करें

1. सभी टर्मिनल और बॉक्स दोबारा जाँचें।
2. पहले कंट्रोलर की बिजली चालू करें।
3. सही डिवाइस ID दिखाई देने की पुष्टि करें।
4. प्लान में शामिल होने पर Wi-Fi या लोकल फोन सेटअप पूरा करें।
5. जाँचें कि टैंक नोड मेन कंट्रोलर से जुड़ रहा है।
6. क्लाउड सुविधा शामिल होने पर ही क्लाउड स्टेटस जाँचें।

### 7. टैंक कैलिब्रेट करें

1. टैंक की क्षमता और उपयोगी ऊँचाई दर्ज करें।
2. स्क्रीन पर दिख रहे स्तर की असली पानी के स्तर से तुलना करें।
3. शांत पानी में रीडिंग जाँचें।
4. कैलिब्रेशन बदलने से पहले सेंसर की जगह और कोण ठीक करें।
5. मंजूर ऑटो स्टार्ट और ऑटो स्टॉप स्तर सेव करें।

### 8. सिस्टम टेस्ट करें

लागू होने वाले सभी टेस्ट पूरे करें:

- टैंक का स्तर सही बदलता है;
- पंप केवल सुरक्षित स्थिति में शुरू होता है;
- पंप तय स्तर पर बंद होता है;
- पानी न होने पर ड्राई-रन सुरक्षा पंप को रोकती है;
- फिजिकल OFF बटन पंप रोकता है;
- टेस्ट अलर्ट दिखाई देते हैं;
- प्लान में होने पर लोकल ऐप चलता है;
- प्लान में होने पर रिमोट/क्लाउड एक्सेस चलता है;
- सभी टैंक, सेंसर और वाल्व मंजूर सेटअप से मेल खाते हैं।

पानी के बिना पंप न चलाएँ और सुरक्षा लॉक को बायपास न करें।

### 9. ग्राहक को जानकारी दें

ग्राहक को दिखाएँ:

- टैंक का स्तर और पंप की स्थिति;
- Auto और Manual मोड का काम;
- फिजिकल OFF बटन का उपयोग;
- ऐप या डैशबोर्ड खोलने का तरीका;
- किन सुविधाओं के लिए इंटरनेट चाहिए;
- राउटर या पासवर्ड बदलने के बाद क्या करना है;
- डिवाइस ID और SaleWell सहायता विवरण।

### 10. समस्या होने पर

- **टैंक स्तर गलत:** सेंसर का कोण, रुकावट, पानी की छींट और कैलिब्रेशन जाँचें।
- **पंप शुरू नहीं होता:** पानी का स्रोत, टैंक स्तर, मोड, सुरक्षा लॉक, कॉन्टैक्टर और बिजली जाँचें।
- **पंप बंद नहीं होता:** फिजिकल OFF दबाएँ या सुरक्षित रूप से बिजली बंद करें। फिर इलेक्ट्रीशियन या SaleWell से संपर्क करें।
- **रिमोट डेटा पुराना है:** कंट्रोलर की बिजली और इंटरनेट जाँचें। लोकल ऑटोमेशन फिर भी चल सकता है।
- **ऐप लोकल कनेक्ट नहीं होता:** कंट्रोलर की बिजली, फोन कनेक्शन, परमिशन और चुना हुआ डिवाइस जाँचें।

सहायता: **support@salewell.co.in** · **+91 87964 52878**
