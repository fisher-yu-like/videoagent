'use strict';

function clonePoint(point) {
  return {t: point.t, x: point.x, y: point.y, visible: point.visible};
}

function prepareDraftPoints(points, primitive) {
  if (!Array.isArray(points)) throw new Error('Draft points must be an array');
  const prepared = points.map(clonePoint);
  const minimum = primitive === 'circle' ? 3 : primitive === 'polyline' ? 2 : 1;
  if (prepared.length < minimum) throw new Error(`Need at least ${minimum} point(s)`);
  if (primitive === 'static' && prepared.length !== 1) throw new Error('Static tracks need exactly one point');
  if (primitive === 'circle') {
    prepared.forEach((point, index) => { point.t = prepared.length === 1 ? 0 : index / (prepared.length - 1); });
  } else if (primitive === 'polyline') {
    for (let index = 1; index < prepared.length; index += 1) {
      if (!(prepared[index].t > prepared[index - 1].t)) {
        throw new Error('Polyline progress values must be strictly increasing and unique');
      }
    }
  }
  return prepared;
}

function updateDraftPoint(points, index, replacement) {
  if (!Number.isInteger(index) || index < 0 || index >= points.length) throw new Error('Invalid draft point index');
  const updated = points.map(clonePoint);
  updated[index] = {...updated[index], x: replacement.x, y: replacement.y};
  return updated;
}

function finishTrack(tracks, selectedIndex, draft, metadata) {
  const next = tracks.map(track => ({...track, target: {...track.target}, points: track.points.map(clonePoint)}));
  const track = {
    track_id: metadata.track_id,
    target: {...metadata.target},
    primitive: metadata.primitive,
    semantic: metadata.semantic,
    points: prepareDraftPoints(draft, metadata.primitive)
  };
  if (Number.isInteger(selectedIndex) && selectedIndex >= 0 && selectedIndex < next.length) next[selectedIndex] = track;
  else next.push(track);
  return next;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {prepareDraftPoints, updateDraftPoint, finishTrack};
}
