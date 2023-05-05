using Microsoft.MixedReality.OpenXR;
using Microsoft.MixedReality.Toolkit;
using Microsoft.MixedReality.Toolkit.SpatialAwareness;
using Microsoft.MixedReality.Toolkit.XRSDK;
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.Assertions;
using UnityEngine.EventSystems;
using UnityEngine.XR;
using UnityEngine.XR.Management;
using UnityEngine.XR.OpenXR;
using Unity.Robotics.ROSTCPConnector;
using RosMessageTypes.BuiltinInterfaces;
using RosMessageTypes.Std;
using RosMessageTypes.Shape;
using RosMessageTypes.Geometry;
using RosMessageTypes.Angel;

#if ENABLE_WINMD_SUPPORT
using Windows.Perception.Spatial;
#endif


public class DetectionSet
{
    public List<Vector3> _leftPoints;
    public List<Vector3> _topPoints;
    public List<Vector3> _rightPoints;
    public List<Vector3> _bottomPoints;
    public List<string> _labels;
    public bool _drawn;

    public DetectionSet(List<Vector3> leftPoints, List<Vector3> topPoints,
                        List<Vector3> rightPoints, List<Vector3> bottomPoints, List<string> labels)
    {
        _leftPoints = leftPoints;
        _topPoints = topPoints;
        _rightPoints = rightPoints;
        _bottomPoints = bottomPoints;
        _labels = labels;
        _drawn = false;
    }

}


public class SpatialMappingCapture : MonoBehaviour, IMixedRealitySpatialAwarenessObservationHandler<SpatialAwarenessMeshObject>
{
    // Ros stuff
    ROSConnection ros;
    public string spatialMapTopicName = "SpatialMapData";
    public string detectionsTopicName = "ObjectDetections3d";

#if ENABLE_WINMD_SUPPORT
    public static SpatialCoordinateSystem unityCoordinateSystem;
#endif

    private IMixedRealitySpatialAwarenessObserver observer;

    UnityEngine.Matrix4x4 worldTransformMatrix;
    private Logger _logger = null;
    private string _debugString = "";

    private List<DetectionSet> _latestDetections = new List<DetectionSet>();
    private List<GameObject> _latestDetectionObjects = new List<GameObject>();

    private static Mutex _mut = new Mutex();

    /// <summary>
    /// Lazy acquire the logger object and return the reference to it.
    /// </summary>
    /// <returns>Logger instance reference.</returns>
    private ref Logger logger()
    {
        if (this._logger == null)
        {
            // TODO: Error handling for null loggerObject?
            this._logger = GameObject.Find("Logger").GetComponent<Logger>();
        }
        return ref this._logger;
    }

    // Start is called before the first frame update
    protected void Start()
    {
        Logger log = logger();

        // Create the image publisher and headset pose publisher
        ros = ROSConnection.GetOrCreateInstance();
        ros.RegisterPublisher<SpatialMeshMsg>(spatialMapTopicName);

        // Create the object detections 3d subscriber and register the callback
        ros.Subscribe<ObjectDetection3dSetMsg>(detectionsTopicName, DetectionCallback);

        // Check that Open XR is available
        var xrSettings = XRGeneralSettings.Instance;
        var xrManager = xrSettings.Manager;
        var xrLoader = xrManager.activeLoader;
        var xrInput = xrLoader.GetLoadedSubsystem<XRInputSubsystem>();
        if (xrInput != null)
        {
            // Set the origin tracking mode and recenter
            xrInput.TrySetTrackingOriginMode(TrackingOriginModeFlags.Device);
            xrInput.TryRecenter();
#if ENABLE_WINMD_SUPPORT
            unityCoordinateSystem = PerceptionInterop.GetSceneCoordinateSystem(UnityEngine.Pose.identity) as SpatialCoordinateSystem;
#endif
            xrInput.trackingOriginUpdated += new System.Action<UnityEngine.XR.XRInputSubsystem>(OriginUpdateHandler);
        }

        observer = CoreServices.GetSpatialAwarenessSystemDataProvider<IMixedRealitySpatialAwarenessObserver>();
        if (observer == null)
        {
            log.LogInfo("Couldn't access Scene Understanding Observer!");
            return;
        }

    }

    private void OriginUpdateHandler(UnityEngine.XR.XRInputSubsystem s)
    {
#if ENABLE_WINMD_SUPPORT
        unityCoordinateSystem = PerceptionInterop.GetSceneCoordinateSystem(UnityEngine.Pose.identity) as SpatialCoordinateSystem;
#endif
    }

    protected void OnEnable()
    {
        CoreServices.SpatialAwarenessSystem.RegisterHandler<IMixedRealitySpatialAwarenessObservationHandler<SpatialAwarenessMeshObject>>(this);
    }

    protected void OnDisable()
    {
        CoreServices.SpatialAwarenessSystem.UnregisterHandler<IMixedRealitySpatialAwarenessObservationHandler<SpatialAwarenessMeshObject>>(this);
    }

    protected void OnDestroy()
    {
        CoreServices.SpatialAwarenessSystem.UnregisterHandler<IMixedRealitySpatialAwarenessObservationHandler<SpatialAwarenessMeshObject>>(this);
    }

    public void OnObservationAdded(MixedRealitySpatialAwarenessEventData<SpatialAwarenessMeshObject> eventData)
    {
        // Copy over the mesh data we need because we cannot access it outside of the main thread
        Vector3[] vertices = eventData.SpatialObject.Filter.mesh.vertices;
        int[] triangles = eventData.SpatialObject.Filter.mesh.triangles;
        int id = eventData.SpatialObject.Id;
        Thread tSendObject = new Thread(() => SendMeshObjectAddition(vertices,
                                                                     triangles,
                                                                     id));
        tSendObject.Start();
    }

    public void OnObservationUpdated(MixedRealitySpatialAwarenessEventData<SpatialAwarenessMeshObject> eventData)
    {
        // Copy over the mesh data we need because we cannot access it outside of the main thread
        Vector3[] vertices = eventData.SpatialObject.Filter.mesh.vertices;
        int[] triangles = eventData.SpatialObject.Filter.mesh.triangles;
        int id = eventData.SpatialObject.Id;
        Thread tSendObject = new Thread(() => SendMeshObjectAddition(vertices,
                                                                     triangles,
                                                                     id));
        tSendObject.Start();
    }

    public void OnObservationRemoved(MixedRealitySpatialAwarenessEventData<SpatialAwarenessMeshObject> eventData)
    {
        // TODO: send a mesh removal message
    }

    // Update is called once per frame
    void Update()
    {
        if (_debugString != "")
        {
            this.logger().LogInfo(_debugString);
            _debugString = "";
        }

        _mut.WaitOne();
        for (int i = 0; i < _latestDetections.Count; i++)
        {
            if (_latestDetections[i]._drawn != true)
            {
                // Have a new detection, so delete the old game objects
                for (int j = 0; j < _latestDetectionObjects.Count; j++)
                {
                    Destroy(_latestDetectionObjects[j]);
                }
                _latestDetectionObjects.Clear();

                for (int j = 0; j < _latestDetections[i]._labels.Count; j++)
                {
                    DrawBox(_latestDetections[i]._leftPoints[j], _latestDetections[i]._topPoints[j],
                            _latestDetections[i]._rightPoints[j], _latestDetections[i]._bottomPoints[j],
                            _latestDetections[i]._labels[j]);

                }
                _latestDetections[i]._drawn = true;
            }
        }
        _mut.ReleaseMutex();
    }

    private void DetectionCallback(ObjectDetection3dSetMsg msg)
    {
        Logger log = logger();
#if ENABLE_WINMD_SUPPORT

        List<string> labels = new List<string>();
        List<Vector3> leftPoints = new List<Vector3>();
        List<Vector3> topPoints = new List<Vector3>();
        List<Vector3> rightPoints = new List<Vector3>();
        List<Vector3> bottomPoints = new List<Vector3>();

        // Convert the ROS point messages to Unity Vector3 objects
        foreach (PointMsg p in msg.left)
        {
            Vector3 point = new Vector3((float)p.x, (float)p.y, (float)p.z);
            leftPoints.Add(point);
        }
        foreach (PointMsg p in msg.top)
        {
            Vector3 point = new Vector3((float)p.x, (float)p.y, (float)p.z);
            topPoints.Add(point);
        }
        foreach (PointMsg p in msg.right)
        {
            Vector3 point = new Vector3((float)p.x, (float)p.y, (float)p.z);
            rightPoints.Add(point);
        }
        foreach (PointMsg p in msg.bottom)
        {
            Vector3 point = new Vector3((float)p.x, (float)p.y, (float)p.z);
            bottomPoints.Add(point);
        }
        foreach (string l in msg.object_labels)
        {
            labels.Add(l);
        }

        DetectionSet d = new DetectionSet(leftPoints, topPoints, rightPoints, bottomPoints, labels);

        _mut.WaitOne();

        // Got a new detection, so clear out the old detections and add the new one
        _latestDetections.Clear();
        _latestDetections.Add(d);

        _mut.ReleaseMutex();
#endif
    }

    private void SendMeshObjectAddition(Vector3[] vertices, int[] triangles, int id)
    {
        List<MeshTriangleMsg> meshTriangles = new List<MeshTriangleMsg>();
        List<PointMsg> meshPoints = new List<PointMsg>();

        // Add triangle vertices to the points message list
        foreach (Vector3 p in vertices)
        {
            meshPoints.Add(new PointMsg(p.x, p.y, p.z));
        }

        // Add triangle indices to the mesh triangles list
        for (int i = 0; i < triangles.Length; i += 3)
        {
            meshTriangles.Add(new MeshTriangleMsg(new uint[3] { Convert.ToUInt32(triangles[i]),
                                                                Convert.ToUInt32(triangles[i + 1]),
                                                                Convert.ToUInt32(triangles[i + 2])
                                                              }
                                                 ));
        }

        MeshMsg shapeMsg = new MeshMsg(meshTriangles.ToArray(), meshPoints.ToArray());

        // Build and publish the spatial mesh message
        SpatialMeshMsg meshMsg = new SpatialMeshMsg(id.ToString(), false, shapeMsg);
        ros.Publish(spatialMapTopicName, meshMsg);
    }

    private void DrawBox(Vector3 cornerLeft, Vector3 cornerTop,
                         Vector3 cornerRight, Vector3 cornerBot,
                         string classTypeStr)
    {
        GameObject lineObject = new GameObject();
        LineRenderer line = lineObject.AddComponent<LineRenderer>();
        line.material.color = Color.white;

        line.startWidth = 0.01f;
        line.endWidth = 0.01f;
        line.positionCount = 5;

        line.SetPosition(0, cornerLeft);
        line.SetPosition(1, cornerTop);
        line.SetPosition(2, cornerRight);
        line.SetPosition(3, cornerBot);
        line.SetPosition(4, cornerLeft);
        line.enabled = true;

        // Draw the label
        GameObject textObject = new GameObject();
        TextMesh textMesh = textObject.AddComponent<TextMesh>();
        MeshRenderer meshRenderer = textObject.GetComponent<MeshRenderer>();
        textMesh.color = Color.blue;
        textMesh.transform.localScale = new Vector3(0.04f, 0.04f, 0.04f);

        textMesh.text = classTypeStr;
        Vector3 textPos = cornerLeft;
        textPos.y = cornerLeft.y + 0.1f;
        textMesh.transform.position = textPos;

        _latestDetectionObjects.Add(lineObject);
        _latestDetectionObjects.Add(textObject);
    }
}