"""Numbered, verbatim source spans selected by the planner, never retyped by it."""
import re
from .source_text import text,canonical,saved_fields


def index(materials):
    result=[];seen=set()
    for material in materials:
        for field,value in saved_fields(material).items():
            # Keep a sentence and its conditions intact; never cut a long sentence.
            parts=re.split(r'\n+|(?<=[.!?])\s+(?=[А-ЯЁA-Z«\d])|\s+(?=[➤👉➡️🔸🔹])',value)
            cursor=0
            for position,part in enumerate(parts):
                part=part.strip();key=(material['material_id'],canonical(part))
                start=value.find(part,cursor);end=start+len(part);cursor=end
                if not part or key in seen:continue
                seen.add(key)
                result.append({'id':f"{material['material_id']}:{field}:{position}",
                               'material_id':material['material_id'],'field':field,'text':part,
                               'start':start,'end':end})
    return result


def expand(result,fragments,materials=None):
    known={f['id']:f for f in fragments};groups=[];errors=[]
    sources={m['material_id']:m for m in materials or []}
    for selection in result.get('groups',[]):
        try:
            focus=[];topic=[]
            for choice in selection['focus']:
                selected=[known[key] for key in choice['fragment_ids']]
                if not selected or any(f['material_id']!=choice['material_id'] for f in selected):raise ValueError('EDITION_FRAGMENT_ID')
                combined=[]
                for fragment in sorted(selected,key=lambda f:(('title','content','description','primary_title','primary_content').index(f['field']),f['start'])):
                    previous=combined[-1] if combined else None
                    consecutive=previous and previous['field']==fragment['field'] and int(fragment['id'].rsplit(':',1)[1])==int(previous['id'].rsplit(':',1)[1])+1
                    material=sources.get(fragment['material_id'])
                    if consecutive and material:
                        combined[-1]={**fragment,'start':previous['start'],
                            'text':saved_fields(material)[fragment['field']][previous['start']:fragment['end']]}
                    else:combined.append(fragment)
                for fragment in combined:
                    focus.append({'material_id':fragment['material_id'],'excerpt':fragment['text'],
                                  'scope':fragment['text'],'summary':choice['summary']})
                topic.append({'material_id':selected[0]['material_id'],'quote':selected[0]['text']})
            ids=list(dict.fromkeys(f['material_id'] for f in focus))
            if set(ids)!=set(selection['material_ids']):raise ValueError('EDITION_FRAGMENT_COVERAGE')
            groups.append({**{k:v for k,v in selection.items() if k!='focus'},'focus':focus,'topic_evidence':topic})
        except (KeyError,TypeError,ValueError) as exc:
            errors.append({'material_ids':selection.get('material_ids',[]),'code':str(exc)})
    return {'groups':groups,'excluded':result.get('excluded',[]),'fragment_errors':errors}
