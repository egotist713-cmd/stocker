"""
export_metadata_contamination_case — будущий regression fixture Export preparation
(docs/EXPORT_PREPARATION_CONTRACT.md §4.5, паспорт §35ZQ).

Синтетический JPEG воспроизводит **классы** загрязнения, найденные в реальном
файле новой сотни (`TOP_1423278419-612x612-wonder 3.5-6x.jpg`): EXIF с
описанием и программой, IPTC (APP13), XMP из многих пространств имён (история
редактора, идентификаторы документа, лицензиар, чужой идентификатор ассета,
digitalSourceType, авторские данные), ICC, комментарий, C2PA / JUMBF,
встроенное видео. Сам сторонний файл в репозиторий не копируется.

inventory(path) перечисляет ВСЁ, что есть в файле, — будущий тест экспорта
проверяет inventory(export) ⊆ whitelist профиля площадки, а не отсутствие
нескольких известных тегов.
"""

import io
import re
import struct
from pathlib import Path

from PIL import Image, ImageCms

SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

XMP = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about=""
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/"
 xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#"
 xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/" xmlns:plus="http://ns.useplus.org/ldf/xmp/1.0/"
 xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
 xmlns:VendorAsset="http://example.com/vendor-asset/1.0/">
<xmp:CreatorTool>Topaz Gigapixel 1.3.6 (Windows)</xmp:CreatorTool>
<xmpMM:DocumentID>xmp.did:0000-source</xmpMM:DocumentID>
<xmpMM:InstanceID>xmp.iid:0000-instance</xmpMM:InstanceID>
<xmpMM:OriginalDocumentID>xmp.did:0000-original</xmpMM:OriginalDocumentID>
<xmpMM:History><rdf:Seq><rdf:li rdf:parseType="Resource"><stEvt:action>saved</stEvt:action>
<stEvt:softwareAgent>Adobe Photoshop Lightroom Classic</stEvt:softwareAgent><stEvt:when>2026-09-27</stEvt:when></rdf:li></rdf:Seq></xmpMM:History>
<photoshop:History>Image processed with Topaz</photoshop:History>
<dc:creator><rdf:Seq><rdf:li>Original Author</rdf:li></rdf:Seq></dc:creator>
<dc:description><rdf:Alt><rdf:li xml:lang="x-default">Source description</rdf:li></rdf:Alt></dc:description>
<xmpRights:WebStatement>https://example.com/license</xmpRights:WebStatement>
<plus:Licensor><rdf:Seq><rdf:li rdf:parseType="Resource"><plus:LicensorURL>https://example.com/licensor</plus:LicensorURL></rdf:li></rdf:Seq></plus:Licensor>
<plus:DataMining>http://ns.useplus.org/ldf/vocab/DMI-PROHIBITED</plus:DataMining>
<Iptc4xmpExt:DigitalSourceType>http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia</Iptc4xmpExt:DigitalSourceType>
<VendorAsset:AssetID>1423278419</VendorAsset:AssetID>
</rdf:Description></rdf:RDF></x:xmpmeta>"""

# Классы загрязнения, которые фикстура обязана содержать (проверяется тестом).
REQUIRED = {
    "segment:APP1:Exif", "segment:APP1:XMP", "segment:APP13:Photoshop", "segment:APP2:ICC",
    "segment:APP11:JUMBF", "segment:COM", "trailing_data",
    "exif:ImageDescription", "exif:Software", "exif:Make", "exif:Model", "exif:Artist", "exif:Copyright",
    "exif:GPSInfo", "exif:BodySerialNumber",
    "iptc:2:80", "iptc:2:110", "iptc:2:116",
    "xmp:xmp:CreatorTool", "xmp:xmpMM:History", "xmp:xmpMM:DocumentID", "xmp:xmpMM:OriginalDocumentID",
    "xmp:stEvt:softwareAgent", "xmp:photoshop:History", "xmp:dc:creator", "xmp:xmpRights:WebStatement",
    "xmp:plus:Licensor", "xmp:plus:LicensorURL", "xmp:plus:DataMining", "xmp:Iptc4xmpExt:DigitalSourceType",
    "xmp:VendorAsset:AssetID",
}


def _segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


def _iptc_irb() -> bytes:
    def dataset(record, number, value):
        return struct.pack(">BBBH", 0x1C, record, number, len(value)) + value

    iptc = dataset(2, 80, b"Original Author") + dataset(2, 110, b"Source Credit") + dataset(2, 116, b"(c) Original Owner")
    block = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" + struct.pack(">I", len(iptc)) + iptc
    if len(iptc) % 2:
        block += b"\x00"
    return b"Photoshop 3.0\x00" + block


def build_contaminated_jpeg(path: Path) -> Path:
    exif = Image.Exif()
    exif[0x010E] = "Source image description"
    exif[0x0131] = "Topaz Gigapixel 1.3.6 (Windows)"
    exif[0x010F] = "SourceCam"
    exif[0x0110] = "Model X"
    exif[0x013B] = "Original Author"
    exif[0x8298] = "(c) Original Owner"
    exif.get_ifd(0x8769)[0xA431] = "SN-123456"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (53.0, 20.0, 12.5)

    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), (120, 130, 140)).save(
        buffer, "JPEG", quality=92, exif=exif, icc_profile=SRGB_ICC, xmp=XMP, comment=b"Processed with Topaz",
    )
    data = buffer.getvalue()
    extra = _segment(0xED, _iptc_irb()) + _segment(0xEB, b"JP\x00\x00\x00\x00\x00\x01jumbc2pa manifest")
    video = (24).to_bytes(4, "big") + b"ftypmp42" + b"\x00" * 12 + b"\x00\x00\x00\x08moov" + b"v" * 2048
    path.write_bytes(data[:2] + extra + data[2:] + video)
    return path


def _segments(data: bytes):
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        yield marker, data[pos + 4:pos + 2 + length]
        if marker == 0xDA:
            return
        pos += 2 + length


def inventory(path: Path) -> set[str]:
    """Всё, что есть в JPEG помимо пикселей: сегменты, теги EXIF, наборы IPTC, свойства XMP, данные после EOI."""
    data = path.read_bytes()
    items = set()
    for marker, payload in _segments(data):
        if marker == 0xE1 and payload.startswith(b"Exif"):
            items.add("segment:APP1:Exif")
        elif marker == 0xE1 and payload.startswith(b"http://ns.adobe.com/xap/1.0/"):
            items.add("segment:APP1:XMP")
            for prefix, name in re.findall(rb"<([A-Za-z0-9]+):([A-Za-z]+)[ >]", payload):
                if prefix not in (b"x", b"rdf"):
                    items.add(f"xmp:{prefix.decode()}:{name.decode()}")
        elif marker == 0xED:
            items.add("segment:APP13:Photoshop")
            for record, number in re.findall(rb"\x1c(.)(.)", payload, re.S):
                items.add(f"iptc:{record[0]}:{number[0]}")
        elif marker == 0xE2 and payload.startswith(b"ICC_PROFILE"):
            items.add("segment:APP2:ICC")
        elif marker == 0xE2 and payload.startswith(b"MPF"):
            items.add("segment:APP2:MPF")
        elif marker == 0xEB:
            items.add("segment:APP11:JUMBF")
        elif marker == 0xFE:
            items.add("segment:COM")
        elif 0xE0 <= marker <= 0xEF and marker != 0xE0:
            items.add(f"segment:APP{marker - 0xE0}")
    if len(data) - (data.rfind(b"\xff\xd9") + 2) > 0:
        items.add("trailing_data")
    from PIL import ExifTags

    with Image.open(path) as image:
        exif = image.getexif()
        for tag in exif:
            items.add(f"exif:{ExifTags.TAGS.get(tag, tag)}")
        for ifd in (0x8769, 0x8825):
            try:
                for tag in exif.get_ifd(ifd):
                    name = ExifTags.GPSTAGS.get(tag, tag) if ifd == 0x8825 else ExifTags.TAGS.get(tag, tag)
                    items.add(f"exif:{name}")
            except KeyError:
                pass
    return items
