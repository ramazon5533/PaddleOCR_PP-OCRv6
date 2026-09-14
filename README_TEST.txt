PP-OCRv6 TEST SERVERLARI
========================

Bu papkadagi serverlar asosiy PP-OCRv4 serverlarning alohida test nusxasi.

Model juftligi:
  PP-OCRv6_medium_det
  PP-OCRv6_medium_rec

O'zgarmagan qismlar:
  - preprocessing va deskew
  - VIN/Barcode filtrlash va validation
  - fallback bosqichlari
  - socket buyruqlari
  - JSON javob formati
  - CPU va MKLDNN sozlamalari
  - portlar

Ishga tushirish:
  Start_All_PP-OCRv6_Servers.bat
  Start_VIN_PP-OCRv6.bat
  Start_BARCODE_PP-OCRv6.bat

MUHIM:
  Asosiy va test server bir xil portlardan foydalanadi. Shuning uchun
  PP-OCRv6 test serverini ochishdan oldin shu portdagi asosiy PP-OCRv4
  serverini to'xtating.

  Birinchi startda PP-OCRv6 medium modellar internet orqali yuklanadi.
  Medium model aniqlikka yo'naltirilgan va PP-OCRv4 mobile_rec dan og'irroq;
  real tezlik va sifat faqat zavod rasmlarida test orqali baholanadi.
