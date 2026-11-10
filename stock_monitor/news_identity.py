"""News aliases only: never remap a trading symbol or certify an ISIN."""
import re
import unicodedata

# Keyed by the owner's watchlist identifiers. Labels are query aliases, not a
# security master; e.g. SpaceX news does not validate the saved SPCX/ISIN.
ALIASES = {
'US88160R1014':['Tesla'], 'US02079K1079':['Alphabet','Google'],
'US11135F1012':['Broadcom'], 'DE0006231004':['Infineon'],
'US0378331005':['Apple'], 'CH0012005267':['Novartis'],
'DE0007500001':['thyssenkrupp'], 'AU000000NST8':['Northern Star Resources','Northern Star'],
'DE000A0D6554':['Nordex'], 'US67066G1040':['Nvidia'],
'US84615Q1031':['SpaceX','Space Exploration Technologies'],
'DK0062498333':['Novo Nordisk'], 'US9311421039':['Walmart'],
'US68389X1054':['Oracle'], 'US5949181045':['Microsoft'],
'FR0000121972':['Schneider Electric'], 'DE000BAY0017':['Bayer'],
'DE0007164600':['SAP'], 'DE0005810055':['Deutsche Börse','Deutsche Boerse'],
'DE000HAG0005':['Hensoldt'], 'US30303M1027':['Meta Platforms','Meta'],
'DE0007236101':['Siemens'], 'FR001400AJ45':['Michelin'],
'US37045V1008':['General Motors'], 'CH0126881561':['Swiss Re'],
'DE0005419105':['Cancom'], 'DE0005439004':['Continental'],
'NL0010273215':['ASML'], 'DE0007030009':['Rheinmetall'],
'DE0008404005':['Allianz'], 'DE000ENER6Y0':['Siemens Energy'],
'NL0000235190':['Airbus'], 'DE0005557508':['Deutsche Telekom'],
'DE0008430026':['Munich Re','Münchener Rück','Muenchener Rueck'],
'DE0005140008':['Deutsche Bank'], 'DE000BASF111':['BASF'],
'DE0007037129':['RWE'], 'DE000ENAG999':['E.ON','Eon'],
'DE0007100000':['Mercedes-Benz','Mercedes Benz'],
'US0079031078':['Advanced Micro Devices','AMD'],
'US69608A1088':['Palantir'], 'US00724F1012':['Adobe'],
'US79466L3024':['Salesforce'], 'FR0000121014':['LVMH','Louis Vuitton'],
'FR0000120321':["L’Oréal","L'Oreal"],
'DE0005190003':['BMW','Bayerische Motoren Werke'],
'DE0005552004':['DHL'], 'US6541061031':['Nike'],
'US0231351067':['Amazon'],
}
AMBIGUOUS={'apple','alphabet','meta','oracle','continental','amazon','northern star','sap','bayer','nike','eon'}
BUSINESS=r'\b(stock|shares?|earnings|revenue|profit|business|company|invest|billion|million|dividend|ceo|cfo|forecast|sales|nasdaq|nyse|dax|aktie|aktien|umsatz|gewinn|konzern|unternehmen|milliard|million|quartal|börs|boers|акци|компани|выручк|прибыл|млрд|млн|iphone|icloud|tim cook|aws|jeff bezos|andy jassy|mark zuckerberg|cloud|software|ai|chips?|tyres?|tires?|swoosh)'

def fold(text):
    text=unicodedata.normalize('NFKD',text.casefold().replace('’',"'"))
    return ''.join(c for c in text if not unicodedata.combining(c))

def aliases(instrument):
    return ALIASES.get(instrument['isin']) or [instrument.get('name','').strip()]

def match(article,instrument,official=False):
    if official:return 'OFFICIAL_ISSUER_FEED'
    text=fold((article.get('title') or '')+' '+(article.get('summary') or ''))
    # An Energy-only story is not automatically attributed to Siemens AG.
    if instrument['isin']=='DE0007236101' and 'siemens energy' in text:
        text=text.replace('siemens energy','')
    for alias in aliases(instrument):
        key=fold(alias)
        if not key or not re.search(r'(?<!\w)'+re.escape(key)+r'(?!\w)',text):continue
        if key in AMBIGUOUS and not re.search(BUSINESS,text,re.I):continue
        return 'NAME_CONTEXT_MATCH_NOT_SECURITY_VALIDATION'
    return None

def query(instrument):
    names=aliases(instrument)[:2]
    return '('+' OR '.join('"'+n.replace('"','')+'"' for n in names if n)+') (company OR stock OR earnings OR business) when:30d'
