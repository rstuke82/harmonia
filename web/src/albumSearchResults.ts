type AlbumResult={other_release?:boolean;type:string;secondary_types:string[]}
export function albumSearchResults<T extends AlbumResult>(rows:T[],showOther:boolean,priority=['Album','EP','Single','Other'],preferStudio=true):T[]{
 const rank=(row:T)=>[Number(!!row.other_release),Number(preferStudio&&row.secondary_types.some(type=>['live','remix','compilation'].includes(type.toLowerCase()))),Math.max(0,priority.findIndex(type=>type.toLowerCase()===(row.type||'Other').toLowerCase()))]
 return rows.filter(row=>showOther||!row.other_release).sort((a,b)=>{const first=rank(a),second=rank(b);return first[0]-second[0]||first[1]-second[1]||first[2]-second[2]})
}
